"""Structured delegation, handoff, failure policy and result validation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from krelcode.agent.turn.budget import BudgetLedger
from krelcode.agent.turn.observation import tool_observation
from krelcode.contracts.application import TaskBudget
from krelcode.contracts.delegation import (
    DelegationFailurePolicy,
    DelegationKind,
    DelegationParentContext,
)
from krelcode.contracts.agent_authority import ApprovalDecision
from krelcode.contracts.tool_execution import ToolExecutionResult
from krelcode.core.events import EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import ProviderCapabilities, StreamFinished, TextDelta
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.authority import build_subagent_authority
from krelcode.subagent.tools import SpawnAgentTool, WaitAgentTool
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.write_file import WriteFileTool


class StructuredProvider:
    name = "structured"
    model = "structured-1"
    capabilities = ProviderCapabilities()

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.tools_seen: list[str] = []
        self.messages_seen: list[Message] = []

    async def stream(self, messages, tools=None):
        self.messages_seen = list(messages)
        self.tools_seen = [
            str(item.get("name") or "")
            for item in (tools or ())
            if isinstance(item, dict)
        ]
        text = json.dumps(self.payload, ensure_ascii=False)
        yield TextDelta(text)
        yield StreamFinished(
            message=Message.assistant(text),
            finish_reason="stop",
            usage={"input_tokens": 3, "output_tokens": 5},
        )


class FlakyProvider:
    name = "flaky"
    model = "flaky-1"
    capabilities = ProviderCapabilities()

    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient provider failure")
        yield TextDelta("recovered")
        yield StreamFinished(
            message=Message.assistant("recovered"),
            finish_reason="stop",
            usage={},
        )


class PolicyProvider:
    name = "policy"
    model = "policy-1"
    capabilities = ProviderCapabilities()

    async def stream(self, messages, tools=None):
        user = next(
            (m.content or "" for m in reversed(messages) if m.role == "user"),
            "",
        )
        objective = next(
            (
                line.removeprefix("Objective:").strip()
                for line in user.splitlines()
                if line.startswith("Objective:")
            ),
            user,
        )
        if "fail" in objective:
            raise RuntimeError("expected child failure")
        if "slow" in objective:
            await asyncio.sleep(0.4)
        text = "ok"
        yield TextDelta(text)
        yield StreamFinished(
            message=Message.assistant(text),
            finish_reason="stop",
            usage={},
        )


def _context(tmp_path: Path, *, budget: TaskBudget | None = None) -> ToolContext:
    return ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=False,
        delegation_parent=DelegationParentContext(
            parent_task_id="turn-1",
            parent_turn_id="turn-1",
            parent_session_id="session-1",
            owner="agent:parent",
            budget=budget or TaskBudget(
                max_steps=20, max_tokens=2_000, max_subagents=2),
        ),
    )


def _manager(tmp_path: Path, provider, *, max_concurrent: int = 4):
    return SubagentManager(
        llm=provider,
        tool_factory=list,
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        max_steps=10,
        max_concurrent=max_concurrent,
    )


def test_structured_delegation_contract_validates_output_and_records_evidence(
    tmp_path,
):
    async def go():
        manager = _manager(tmp_path, StructuredProvider({
            "summary": "implemented",
            "changed_paths": ["src/a.py"],
            "checks": ["focused test passes"],
        }))
        context = _context(tmp_path)
        spawn = SpawnAgentTool(manager)
        result = await spawn.execute({
            "delegation": {
                "objective": "Implement bounded fix",
                "scope": {"write_paths": ["src/a.py"]},
                "known_context": {
                    "facts": ["parser drops commas"],
                    "assumptions": ["no API change"],
                },
                "capabilities": {"definition": "general-purpose"},
                "output": {
                    "format": "json",
                    "required_fields": [
                        "summary", "changed_paths", "checks",
                    ],
                    "schema": {
                        "type": "object",
                        "required": [
                            "summary", "changed_paths", "checks",
                        ],
                    },
                },
                "acceptance": {
                    "criteria": ["focused behavior is verified"],
                    "required_evidence": ["subagent-result"],
                },
                "failure_policy": "best_effort",
            },
        }, context)
        assert result.is_error is False
        agent_id = result.data["agent_id"]
        waited = await WaitAgentTool(manager).execute(
            {"agent_id": agent_id, "timeout": 5}, context)

        assert waited.is_error is False
        delegation_result = waited.data["delegation_result"]
        assert delegation_result["accepted"] is True
        assert delegation_result["agent_id"] == agent_id
        assert delegation_result["evidence"]
        assert delegation_result["structured_output"]["summary"] == "implemented"
        assert any(
            "parser drops commas" in (message.content or "")
            for message in manager.llm.messages_seen
        )
        await manager.shutdown()

    asyncio.run(go())


def test_retry_or_reassign_recovers_transient_child_failure(tmp_path):
    async def go():
        provider = FlakyProvider()
        manager = _manager(tmp_path, provider)
        context = _context(tmp_path)
        spawned = await SpawnAgentTool(manager).execute({
            "delegation": {
                "objective": "recover",
                "failure_policy": "retry_or_reassign",
                "retry_policy": {
                    "max_retries": 1,
                    "allow_reassign": True,
                    "reassign_definition": "general-purpose",
                },
                "acceptance": {"required_evidence": []},
            },
        }, context)
        waited = await WaitAgentTool(manager).execute(
            {"agent_id": spawned.data["agent_id"], "timeout": 5}, context)

        assert provider.calls >= 2
        assert waited.is_error is False
        assert waited.data["policy_actions"]
        assert waited.data["delegation_result"]["accepted"] is True
        await manager.shutdown()

    asyncio.run(go())


def test_invalid_output_schema_rejects_child_result(tmp_path):
    async def go():
        manager = _manager(tmp_path, StructuredProvider({"summary": "incomplete"}))
        context = _context(tmp_path)
        spawned = await SpawnAgentTool(manager).execute({
            "delegation": {
                "objective": "return strict output",
                "output": {
                    "format": "json",
                    "required_fields": ["summary", "checks"],
                    "schema": {
                        "type": "object",
                        "required": ["summary", "checks"],
                    },
                },
                "acceptance": {"required_evidence": []},
            },
        }, context)
        waited = await WaitAgentTool(manager).execute(
            {"agent_id": spawned.data["agent_id"], "timeout": 5}, context)
        assert waited.is_error is True
        result = waited.data["delegation_result"]
        assert result["accepted"] is False
        assert any(
            item.startswith("missing_output_fields")
            or item.startswith("output_schema:")
            for item in result["validation_errors"]
        )
        await manager.shutdown()

    asyncio.run(go())


def test_result_rejects_workspace_changed_after_delegation_started(tmp_path):
    async def go():
        manager = _manager(tmp_path, StructuredProvider({"summary": "done"}))
        context = _context(tmp_path)
        spawned = await SpawnAgentTool(manager).execute({
            "delegation": {
                "objective": (
                    "determine whether this delegated implementation result "
                    "is stale before accepting it"
                ),
                "output": {"format": "json"},
                "acceptance": {
                    "required_evidence": [],
                    "require_workspace_match": True,
                },
            },
        }, context)
        handle = manager.get(spawned.data["agent_id"])
        assert handle is not None
        await manager.wait(handle.id, timeout=5)
        (tmp_path / "changed-after-child.py").write_text(
            "changed = True\n", encoding="utf-8")
        waited = await WaitAgentTool(manager).execute(
            {"agent_id": handle.id, "timeout": 5}, context)
        assert waited.is_error is True
        assert any(
            "workspace_changed_during_delegation" in item
            for item in waited.data["delegation_result"]["validation_errors"]
        )
        await manager.shutdown()

    asyncio.run(go())


def test_handoff_records_responsibility_and_requires_recovery_checkpoint(
    tmp_path,
):
    async def go():
        manager = _manager(tmp_path, StructuredProvider({
            "summary": "continued",
        }))
        events = []
        manager.bus.subscribe(events.append)
        context = _context(tmp_path)
        tool = SpawnAgentTool(manager)

        invalid = await tool.execute({
            "delegation": {
                "objective": "continue",
                "relation_kind": "handoff",
                "handoff_state": {"remaining": "finish"},
            },
        }, context)
        assert invalid.is_error is True
        assert "recovery_checkpoint_id" in invalid.content

        valid = await tool.execute({
            "delegation": {
                "objective": "continue",
                "relation_kind": "handoff",
                "recovery_checkpoint_id": "cp-1",
                "handoff_state": {"remaining": "finish"},
                "output": {"format": "json"},
                "acceptance": {"required_evidence": []},
            },
        }, context)
        assert valid.is_error is False
        handle = manager.get(valid.data["agent_id"])
        assert handle is not None
        assert handle.delegation_relation is not None
        assert handle.delegation_relation.kind is DelegationKind.HANDOFF
        assert handle.responsibility_owner == f"subagent:{handle.id}"
        spawned_event = next(
            event for event in events
            if getattr(event, "agent_id", "") == handle.id
            and event.__class__.__name__ == "SubagentSpawned"
        )
        assert spawned_event.relation_kind == "handoff"
        assert spawned_event.responsibility_owner == f"subagent:{handle.id}"
        await manager.wait(handle.id, timeout=5)
        assert handle.responsibility_owner == "agent:parent"
        await manager.shutdown()

    asyncio.run(go())


def test_parent_budget_receives_child_usage(tmp_path):
    async def go():
        ledger = BudgetLedger(TaskBudget(
            max_steps=20, max_tokens=2_000, max_subagents=2))
        context = _context(tmp_path)
        context.delegation_usage_recorder = ledger.record_external_usage
        manager = _manager(tmp_path, StructuredProvider({"summary": "done"}))
        spawned = await SpawnAgentTool(manager).execute({
            "delegation": {
                "objective": (
                    "inspect delegated child usage accounting and budget rollup"
                ),
                "output": {"format": "json"},
                "acceptance": {"required_evidence": []},
            },
        }, context)
        await WaitAgentTool(manager).execute(
            {"agent_id": spawned.data["agent_id"], "timeout": 5}, context)
        assert ledger.usage_record()["steps"] >= 1
        await manager.shutdown()

    asyncio.run(go())


def test_structured_capabilities_narrow_child_tool_pool(tmp_path):
    async def go():
        provider = StructuredProvider({"summary": "done"})
        manager = SubagentManager(
            llm=provider,
            tool_factory=lambda: [ReadFileTool(), WriteFileTool()],
            tool_context=ToolContext(
                workspace_root=tmp_path, sandbox_enabled=False),
            policy=AllowAllPolicy(),
            bus=EventBus(),
            max_steps=10,
        )
        context = _context(tmp_path)
        spawned = await SpawnAgentTool(manager).execute({
            "delegation": {
                "objective": (
                    "inspect one bounded file with a specialized read-only role"
                ),
                "capabilities": {
                    "allowed_tools": ["read_file"],
                    "permission_mode": "read-only",
                },
                "output": {"format": "json"},
                "acceptance": {"required_evidence": []},
            },
        }, context)
        assert spawned.is_error is False
        await WaitAgentTool(manager).execute(
            {"agent_id": spawned.data["agent_id"], "timeout": 5}, context)
        assert "read_file" in provider.tools_seen
        assert "write_file" not in provider.tools_seen
        handle = manager.get(spawned.data["agent_id"])
        assert handle is not None
        assert handle.permission_mode == "read-only"
        await manager.shutdown()

    asyncio.run(go())


def test_delegation_value_and_overlapping_write_scope_are_rejected(tmp_path):
    async def go():
        manager = _manager(tmp_path, PolicyProvider())
        context = _context(tmp_path)
        tool = SpawnAgentTool(manager)

        trivial = await tool.execute({
            "delegation": {"objective": "do it"},
        }, context)
        assert trivial.is_error is True
        assert "委派价值不足" in trivial.content

        first = await tool.execute({
            "delegation": {
                "objective": "perform slow implementation work in shared scope",
                "scope": {"write_paths": ["src/shared"]},
                "acceptance": {"required_evidence": []},
            },
        }, context)
        assert first.is_error is False
        overlapping = await tool.execute({
            "delegation": {
                "objective": "perform another implementation task in same area",
                "scope": {"write_paths": ["src/shared/module.py"]},
                "acceptance": {"required_evidence": []},
            },
        }, context)
        assert overlapping.is_error is True
        assert "写入范围" in overlapping.content
        await manager.shutdown()

    asyncio.run(go())


def test_delegation_scope_builds_narrow_authority_envelope(tmp_path):
    from krelcode.contracts.delegation import (
        DelegationContract,
        DelegationScope,
    )

    contract = DelegationContract(
        objective="bounded implementation with explicit write scope",
        scope=DelegationScope(write_paths=("src/allowed/**",)),
    )
    authority = build_subagent_authority(
        contract,
        tools=[SimpleNamespace(name="write_file")],
        mcp_servers=(),
        network_allowed=False,
    )
    allowed = authority.evaluate_tool(
        "write_file", {"path": "src/allowed/module.py"})
    denied = authority.evaluate_tool(
        "write_file", {"path": "src/other/module.py"})
    assert allowed.decision is ApprovalDecision.ALLOW
    assert denied.decision is ApprovalDecision.DENY


def test_fail_fast_stops_active_siblings_and_best_effort_keeps_success(
    tmp_path,
):
    async def go():
        manager = _manager(tmp_path, PolicyProvider())
        context = _context(tmp_path)
        tool = SpawnAgentTool(manager)
        failed = await tool.execute({
            "delegation": {
                "objective": "fail now",
                "failure_policy": "fail_fast",
                "acceptance": {"required_evidence": []},
            },
        }, context)
        slow = await tool.execute({
            "delegation": {
                "objective": "slow work",
                "failure_policy": "fail_fast",
                "acceptance": {"required_evidence": []},
            },
        }, context)

        report = await WaitAgentTool(manager).execute({
            "agent_id": (
                f"{failed.data['agent_id']},{slow.data['agent_id']}"),
            "timeout": 5,
        }, context)
        assert report.is_error is True
        assert any(
            action.startswith("fail_fast:kill:")
            for action in report.data["policy_actions"]
        )

        best_manager = _manager(tmp_path, PolicyProvider())
        best_tool = SpawnAgentTool(best_manager)
        bad = await best_tool.execute({
            "delegation": {
                "objective": (
                    "perform noncritical exploration that may fail without "
                    "blocking the parent task"
                ),
                "failure_policy": "best_effort",
                "acceptance": {"required_evidence": []},
            },
        }, context)
        good = await best_tool.execute({
            "delegation": {
                "objective": (
                    "perform successful bounded exploration for the parent task"
                ),
                "failure_policy": "best_effort",
                "acceptance": {"required_evidence": []},
            },
        }, context)
        best = await WaitAgentTool(best_manager).execute({
            "agent_id": f"{bad.data['agent_id']},{good.data['agent_id']}",
            "timeout": 5,
        }, context)
        assert best.is_error is False
        assert best.data["accepted"][good.data["agent_id"]] is True
        await manager.shutdown()
        await best_manager.shutdown()

    asyncio.run(go())


def test_escalation_waits_for_user_decision(tmp_path):
    async def go():
        manager = _manager(tmp_path, PolicyProvider())
        context = _context(tmp_path)
        requested = []

        async def request_input(prompt, options, allow_free_text=True):
            requested.append((prompt, options, allow_free_text))
            return "partial"

        context.input_requester = request_input
        spawned = await SpawnAgentTool(manager).execute({
            "delegation": {
                "objective": "fail and escalate",
                "failure_policy": "escalate",
                "acceptance": {"required_evidence": []},
            },
        }, context)
        waited = await WaitAgentTool(manager).execute(
            {"agent_id": spawned.data["agent_id"], "timeout": 5}, context)
        assert requested
        assert waited.data["escalated"] is True
        assert "escalate:partial" in waited.data["policy_actions"]
        await manager.shutdown()

    asyncio.run(go())


def test_subagent_observation_covers_all_control_tools_and_evidence():
    for tool_name in (
        "spawn_agent", "wait_agent", "send_to_agent",
        "get_agent_feed", "list_agents", "pause_agent",
        "resume_agent", "kill_agent",
    ):
        observation = tool_observation(
            turn_id="turn-1",
            tool_call=ToolCall("call-1", tool_name, {}),
            result=ToolExecutionResult(
                content="child evidence",
                data={
                    "delegation_result": {
                        "evidence": [{"id": "sidechain:a1"}],
                    },
                },
            ),
        )
        assert observation.source.value == "subagent"
        assert observation.trust == "untrusted"
        assert observation.requires_validation is True
        assert "sidechain:a1" in observation.evidence_ids
