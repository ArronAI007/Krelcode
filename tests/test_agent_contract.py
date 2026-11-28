"""Unified Agent Contract, authority, deliverable and acceptance gates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from krelcode.agent.turn.acceptance import AcceptanceGateError
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.runtime import AgentRuntime
from krelcode.application.acceptance import DefaultAcceptanceEvaluator
from krelcode.contracts.agent import (
    AgentContractFactory,
    ApprovalDecision,
    AuthorityEnvelope,
    DeliverableContract,
    DeliverableKind,
    DeliverableRequirement,
    InputContract,
    InputKind,
    InputRequirement,
)
from krelcode.contracts.application import (
    AcceptanceContract,
    ApplicationRequestFactory,
    RiskLevel,
    RiskProfile,
)
from krelcode.contracts.runtime import RuntimeSandboxBinding
from krelcode.contracts.runtime import RuntimeTask
from krelcode.core.events import RunStarted
from krelcode.core.messages import Message, ToolCall
from krelcode.governance.control_plane import DefaultControlPlane
from krelcode.llm.providers.mock import MockProvider
from krelcode.runtime.execution import LocalTaskRuntime
from krelcode.runtime.execution.task_state_store import (
    task_from_mapping,
    task_to_mapping,
)
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.stream_json import parse_input_line
from krelcode.storage import LocalArtifactStore
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class WriteTool:
    name = "write_file"
    description = "write a file"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, args, ctx):
        path = Path(ctx.workspace_root) / str(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        return ToolResult(content=f"wrote {args['path']}")


def _application(
    goal: str = "implement",
    *,
    acceptance: AcceptanceContract | None = None,
    risk: RiskProfile | None = None,
):
    return ApplicationRequestFactory.from_input_source(
        "sdk",
        goal=goal,
        acceptance=acceptance,
        risk=risk,
    )


def test_agent_contract_hash_round_trip_and_risk_approval() -> None:
    contract = AgentContractFactory.from_application(
        _application(risk=RiskProfile(level=RiskLevel.HIGH)),
        sandbox=RuntimeSandboxBinding(),
    )

    restored = type(contract).from_dict(contract.to_dict())

    assert restored == contract
    assert restored.contract_hash
    assert restored.contract_id.startswith("agent-")
    assert any(
        rule.decision is ApprovalDecision.REQUIRE_APPROVAL
        for rule in restored.authority.approval_rules
    )


def test_agent_contract_rejects_missing_required_input() -> None:
    contract = AgentContractFactory.from_application(
        _application(),
        inputs=InputContract(requirements=(
            InputRequirement("goal", InputKind.TEXT),
            InputRequirement(
                "screenshot",
                InputKind.IMAGE,
                media_types=("image/png",),
            ),
        )),
    )

    errors = contract.validate_inputs("inspect this", ())

    assert errors
    assert "screenshot" in errors[0]


def test_stream_json_accepts_explicit_agent_contract() -> None:
    contract = AgentContractFactory.from_application(
        _application("stream contract"))

    parsed = parse_input_line(json.dumps({
        "type": "user",
        "content": "stream contract",
        "request_id": "r1",
        "agent_contract": contract.to_dict(),
    }))

    assert parsed.application == contract.intent
    assert parsed.agent_contract == contract


def test_runtime_task_contract_round_trips_for_resume() -> None:
    contract = AgentContractFactory.from_application(
        _application("persist contract"))
    task = RuntimeTask(
        turn_id="turn-1",
        message="persist contract",
        application=contract.intent,
        identity=contract.principal,
        harness_release=contract.harness_release,
        sandbox=contract.sandbox,
        agent_contract=contract,
    )

    restored = task_from_mapping(task_to_mapping(task))

    assert restored.agent_contract == contract
    assert restored.contract.contract_hash == contract.contract_hash


def test_query_engine_freezes_contract_before_runtime_task(tmp_path: Path) -> None:
    loop = AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    events = []
    loop.bus.subscribe(events.append)
    request = _application("freeze contract")

    asyncio.run(AgentRuntime(
        loop,
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    ).run_once("freeze contract", application_request=request))

    started = next(event for event in events if isinstance(event, RunStarted))
    assert started.agent_contract["contract_hash"]
    assert loop.agent_contract is not None
    assert loop.agent_contract.intent == request


def test_contract_denied_tool_is_not_executed(tmp_path: Path) -> None:
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("w1", "write_file", {
                    "path": "forbidden.txt",
                    "content": "x",
                }),
            ]),
            "denied",
        ]),
        registry=ToolRegistry([WriteTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    contract = AgentContractFactory.from_application(
        _application("do not write"),
        authority=AuthorityEnvelope(
            allowed_systems=("workspace",),
            denied_tools=("write_file",),
        ),
    )

    reply = asyncio.run(AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    ).run_once("do not write", agent_contract=contract))

    assert reply == "denied"
    assert not (tmp_path / "forbidden.txt").exists()
    tool_message = next(
        message for message in loop.history if message.role == "tool")
    assert "工具被契约禁止" in tool_message.content


def test_high_risk_contract_requires_real_approval(tmp_path: Path) -> None:
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("w1", "write_file", {
                    "path": "approval.txt",
                    "content": "x",
                }),
            ]),
            "approval missing",
        ]),
        registry=ToolRegistry([WriteTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    contract = AgentContractFactory.from_application(
        _application(
            "write after approval",
            risk=RiskProfile(level=RiskLevel.HIGH),
        ),
    )

    asyncio.run(AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    ).run_once("write after approval", agent_contract=contract))

    assert not (tmp_path / "approval.txt").exists()
    tool_message = next(
        message for message in loop.history if message.role == "tool")
    assert "要求人工审批" in tool_message.content


def test_acceptance_gate_blocks_failed_delivery(tmp_path: Path) -> None:
    loop = AgentLoop(
        llm=MockProvider(scripted=["implemented"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    runtime = AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    )
    request = _application(
        "fix tests",
        acceptance=AcceptanceContract(
            criteria=("tests pass",),
            required_evidence=("tests",),
        ),
    )

    with pytest.raises(AcceptanceGateError):
        asyncio.run(runtime.run_once(
            "fix tests",
            application_request=request,
        ))
    assert loop.acceptance_result is not None
    assert not loop.acceptance_result.passed


def test_deliverable_file_is_bound_to_artifact_evidence(
    tmp_path: Path,
) -> None:
    artifact_store = LocalArtifactStore(tmp_path / ".krelcode" / "artifacts")
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("w1", "write_file", {
                    "path": "report.txt",
                    "content": "verified",
                }),
            ]),
            "report ready",
        ]),
        registry=ToolRegistry([WriteTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        artifact_store=artifact_store,
        system_prompt="SYS",
    )
    contract = AgentContractFactory.from_application(
        _application("create report"),
        deliverables=DeliverableContract(requirements=(
            DeliverableRequirement(
                "report",
                DeliverableKind.FILE,
                path="report.txt",
                media_type="text/plain",
                artifact_required=True,
            ),
            DeliverableRequirement(
                "final_response",
                DeliverableKind.RESPONSE,
            ),
        )),
    )

    asyncio.run(AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    ).run_once("create report", agent_contract=contract))

    assert loop.acceptance_result is not None
    assert loop.acceptance_result.passed
    report_evidence = next(
        item for item in loop.acceptance_result.evidence
        if item.deliverable_id == "report"
    )
    assert report_evidence.artifact is not None
    assert report_evidence.digest
    assert artifact_store.list()
