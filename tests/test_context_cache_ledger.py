"""P1: unified request/cache ledger across loop, resume and subagents."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.context_cache_ledger import ContextCacheLedger
from krelcode.agent.context_plan import ContextPlanSnapshot, history_digest
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_plan import (
    PromptFragmentSnapshot,
    PromptMemoryHitSnapshot,
    PromptPlanSnapshot,
)
from krelcode.agent.query import QueryEvent
from krelcode.core.events import ContextAssembled, EventBus
from krelcode.core.messages import Message
from krelcode.llm.base import (
    PreparedLLMRequest,
    ProviderCapabilities,
    StreamFinished,
)
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_context
from krelcode.session.commands.types import CommandRequest
from krelcode.session.context_checkpoint import (
    ContextCheckpointStore,
    hash_tracked_files,
    restore_context_checkpoint,
)
from krelcode.session.memory import MemoryKind, MemoryScope, MemoryStore
from krelcode.session.state import SessionViewState
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.sidechain import read_sidechain
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.tui.panels import build_panels


def _prompt_plan(**changes) -> PromptPlanSnapshot:
    values = dict(
        turn_id="turn-ledger",
        step=1,
        engine="v3",
        fragments=(
            PromptFragmentSnapshot(
                id="project:rules",
                source="project:rules",
                scope="project",
                layer="L3_PROJECT_RULES",
                stability="static",
                trust="project",
                tokens=12,
                cacheable=True,
                stable=True,
                content_hash="rule-content-hash",
                provenance={"source_role": "instruction"},
            ),
            PromptFragmentSnapshot(
                id="memory:scoped",
                source="memory",
                scope="project",
                layer="L7_MEMORY_EVIDENCE",
                stability="turn",
                trust="untrusted",
                tokens=8,
                cacheable=False,
                stable=False,
                content_hash="memory-content-hash",
                provenance={
                    "source_role": "evidence",
                    "evidence_type": "memory",
                    "requires_validation": True,
                },
            ),
        ),
        stable_hash="stable-1",
        dynamic_hash="dynamic-1",
        full_hash="full-1",
        instruction_hash="instruction-1",
        evidence_hash="evidence-1",
        history_digest="history-1",
        tool_catalog_hash="tools-1",
        tool_count=2,
        memory_hits=(PromptMemoryHitSnapshot(
            id="mem-1", source="memory", scope="project", tokens=8),),
    )
    values.update(changes)
    return PromptPlanSnapshot(**values)


def _ledger(**changes) -> ContextCacheLedger:
    direct_keys = {
        "tool_catalog_hash", "history_digest", "memory_evidence_ids",
    }
    direct = {
        key: value for key, value in changes.items() if key in direct_keys
    }
    component_changes = {
        key: value for key, value in changes.items() if key not in direct_keys
    }
    values = dict(
        prompt_plan=_prompt_plan(),
        attachment_snapshot=SimpleNamespace(
            generation=3,
            items=(
                SimpleNamespace(
                    id="attachment:environment_snapshot",
                    revision=2,
                    semantic_key="semantic-v2",
                ),
            ),
        ),
        compact_checkpoint_id="compact-1",
        working_context_sequence=7,
        provider="mock",
        model="mock-model",
        provider_request_digest="wire-1",
        wire_fidelity="exact",
    )
    values.update(component_changes)
    ledger = ContextCacheLedger.from_components(**values)
    if direct:
        ledger = replace(ledger, complete_request_hash="", **direct)
    return ledger


def test_ledger_round_trip_is_content_free_and_preserves_identity():
    ledger = _ledger()
    restored = ContextCacheLedger.from_dict(ledger.as_dict())

    assert restored == ledger
    assert restored.complete_request_hash == ledger.complete_request_hash
    assert restored.instruction_hash == "instruction-1"
    assert restored.evidence_hash == "evidence-1"
    assert restored.memory_evidence_ids == ("mem-1",)
    assert restored.attachment_generation == 3
    assert restored.attachment_revisions == (
        ("attachment:environment_snapshot", 2),)
    encoded = json.dumps(restored.as_dict(), ensure_ascii=False)
    assert "rule-content-hash" not in encoded
    assert "memory-content-hash" not in encoded
    assert "PROJECT RULE BODY" not in encoded


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"tool_catalog_hash": "tools-2"}, "tool_catalog_changed"),
        ({"history_digest": "history-2"}, "history_digest_changed"),
        ({"working_context_sequence": 8}, "working_context_changed"),
        ({"compact_checkpoint_id": "compact-2"}, "compact_checkpoint_changed"),
        ({"memory_evidence_ids": ("mem-2",)}, "memory_evidence_changed"),
        ({"provider_request_digest": "wire-2"}, "provider_request_digest_changed"),
    ],
)
def test_ledger_diff_explains_cache_miss_without_changing_hashing(changes, reason):
    previous = _ledger()
    current = _ledger(**changes).compared_with(previous)

    assert current.diff.available
    assert current.diff.cache_miss
    assert reason in current.diff.reasons
    assert current.complete_request_hash != previous.complete_request_hash


def test_ledger_diff_distinguishes_attachments_and_policy_inheritance():
    previous = _ledger()
    attachment_changed = _ledger(
        attachment_snapshot=SimpleNamespace(
            generation=4,
            items=(
                SimpleNamespace(
                    id="attachment:environment_snapshot",
                    revision=3,
                    semantic_key="semantic-v3",
                ),
            ),
        ),
    ).compared_with(previous)
    assert attachment_changed.diff.attachments_changed
    assert "environment_attachments_changed" in attachment_changed.diff.reasons

    matching = _ledger(
        parent_policy_instruction_hash=previous.policy_instruction_hash)
    mismatching = _ledger(parent_policy_instruction_hash="other-policy")
    assert matching.instruction_inheritance == "match"
    assert mismatching.instruction_inheritance == "mismatch"


class PreparedProvider:
    name = "prepared-test"
    model = "prepared-model"
    capabilities = ProviderCapabilities()

    def __init__(self):
        self.prepared: PreparedLLMRequest | None = None

    def prepare_request(self, messages, tools=None):
        body = json.dumps({
            "messages": [
                {"role": message.role, "content": message.content or ""}
                for message in messages
            ],
            "tools": list(tools or ()),
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.prepared = PreparedLLMRequest(
            provider=self.name,
            model=self.model,
            url="https://example.invalid/v1",
            headers={},
            body=body,
            request_body_tokens=20,
            payload_digest=hashlib.sha256(body).hexdigest(),
            cache_mode="none",
        )
        return self.prepared

    async def stream_prepared(self, prepared):
        yield StreamFinished(Message.assistant("done"), "stop", {})

    async def stream(self, messages, tools=None):
        prepared = self.prepare_request(messages, tools)
        async for event in self.stream_prepared(prepared):
            yield event


def _make_loop(tmp_path: Path, llm) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
    )


def test_real_request_builds_ledger_before_ui_event(tmp_path):
    provider = PreparedProvider()
    loop = _make_loop(tmp_path, provider)
    events = []
    loop.bus.subscribe(events.append)

    assert asyncio.run(loop.run("question", turn_id="turn-real")) == "done"
    ledger = loop.last_context_cache_ledger
    assert ledger is not None
    assert provider.prepared is not None
    assert ledger.owner == "main"
    assert ledger.provider == provider.name
    assert ledger.model == provider.model
    assert ledger.provider_request_digest == provider.prepared.payload_digest
    assert ledger.wire_fidelity == "exact"
    assert ledger.complete_request_hash
    assert loop.last_context_plan is not None
    assert loop.last_context_plan.cache_ledger == ledger

    assembled = next(event for event in events if isinstance(event, ContextAssembled))
    assert assembled.ledger is not None
    assert assembled.ledger["complete_request_hash"] == ledger.complete_request_hash
    assert assembled.ledger["tool_catalog_hash"] == ledger.tool_catalog_hash

    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="ledger-session",
        cwd=str(tmp_path),
        provider=provider.name,
        model=provider.model,
        run_mode="stream-json",
    ))
    encoded = encoder.encode(QueryEvent(
        turn_id="turn-real", sequence=1, event=assembled))
    assert encoded["ledger"]["complete_request_hash"] == ledger.complete_request_hash


def test_resume_preserves_ledger_and_records_invalidations(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old = True\n", encoding="utf-8")
    history = [
        Message.system("SYSTEM"),
        Message.user("finish app.py"),
        Message.assistant("working"),
    ]
    ledger = _ledger()
    plan = ContextPlanSnapshot(
        turn_id="turn-old",
        step=1,
        history_digest=history_digest(history),
        working_context_sequence=7,
        cache_ledger=ledger,
    )
    store = ContextCheckpointStore(tmp_path)
    checkpoint = store.record(
        history=history,
        session_id="s",
        turn_id="turn-old",
        file_hashes=hash_tracked_files((target,), tmp_path),
        context_plan=plan,
    )
    assert checkpoint is not None
    target.write_text("old = False\n", encoding="utf-8")

    loop = _make_loop(tmp_path, MockProvider())
    result = restore_context_checkpoint(loop, checkpoint)

    restored = result.context_plan.cache_ledger
    assert restored.complete_request_hash == ledger.complete_request_hash
    assert restored.restored_from_checkpoint_id == checkpoint.id
    assert loop.last_context_cache_ledger == restored
    assert "workspace-refresh" in restored.resume_invalidations
    assert "file-integrity-changed" in restored.resume_invalidations


def test_resume_records_memory_selection_refresh(tmp_path):
    memory = MemoryStore(
        tmp_path,
        path=tmp_path / "memory.jsonl",
        user_path=tmp_path / "user-memory.jsonl",
    )
    entry = memory.add(
        "login tests use pytest",
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.KNOWLEDGE,
    )
    history = [Message.system("SYSTEM"), Message.user("fix login")]
    checkpoint = ContextCheckpointStore(tmp_path).record(
        history=history,
        session_id="s",
        turn_id="turn-old",
        memory_query="login pytest",
        context_plan=ContextPlanSnapshot(
            history_digest=history_digest(history),
            cache_ledger=_ledger(memory_evidence_ids=()),
        ),
    )
    assert checkpoint is not None
    loop = _make_loop(tmp_path, MockProvider())
    loop.memory_store = memory

    result = restore_context_checkpoint(
        loop, checkpoint, memory_store=memory)

    assert entry.id in result.memory_ids
    assert "memory-selection-refreshed" in (
        result.context_plan.cache_ledger.resume_invalidations)


class EchoProvider:
    name = "echo"
    model = "echo-model"
    capabilities = ProviderCapabilities()

    async def stream(self, messages, tools=None):
        yield StreamFinished(Message.assistant("child done"), "stop", {})


def test_subagent_ledger_is_persisted_with_parent_policy_diagnosis(tmp_path):
    parent_ledger = ContextCacheLedger(
        provider="parent",
        model="parent-model",
        stable_hash="parent-stable",
        policy_instruction_hash="parent-policy",
        provider_request_digest="parent-wire",
    )

    async def go():
        manager = SubagentManager(
            llm=EchoProvider(),
            tool_factory=list,
            tool_context=ToolContext(workspace_root=tmp_path),
            policy=AllowAllPolicy(),
            bus=EventBus(),
            cache_ledger_provider=lambda: parent_ledger,
        )
        handle = await manager.spawn("inspect cache ledger")
        handle = await manager.wait("a1", timeout=5)
        assert handle.status == "completed"
        assert handle.sidechain_path is not None
        records = read_sidechain(handle.sidechain_path)
        ledger_records = [
            record for record in records
            if record.get("type") == "context_cache_ledger"
        ]
        assert ledger_records
        child = ContextCacheLedger.from_dict(ledger_records[-1]["ledger"])
        assert child.owner == "subagent:a1"
        assert child.parent_policy_instruction_hash == "parent-policy"
        assert child.instruction_inheritance in {"match", "mismatch"}
        await manager.shutdown()

    asyncio.run(go())


def test_context_ledger_command_reports_miss_and_resume_diagnostics(tmp_path):
    previous = _ledger()
    ledger = _ledger(
        provider_request_digest="wire-2",
        history_digest="history-2",
        parent_policy_instruction_hash="parent-policy",
    ).compared_with(previous)
    ledger = ledger.restored(
        "checkpoint-1",
        invalidations=("workspace-refresh", "file-integrity-changed"),
    )
    loop = _make_loop(tmp_path, MockProvider())
    loop.last_context_plan = ContextPlanSnapshot(cache_ledger=ledger)
    loop.last_context_cache_ledger = ledger
    context = CommandContext(
        request=CommandRequest(
            name="context",
            arguments="ledger",
            raw_text="/context ledger",
        ),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=tmp_path,
    )

    output = "\n".join(asyncio.run(handle_context(context)).output)

    assert "[cache ledger]" in output
    assert "cache miss: 是" in output
    assert "history_digest_changed" in output
    assert "provider_request_digest" in output
    assert "file-integrity-changed" in output
    assert "inheritance mismatch" in output
    assert "SYSTEM" not in output


def test_tui_context_panel_surfaces_ledger_without_content(tmp_path):
    previous = _ledger()
    ledger = _ledger(history_digest="history-2").compared_with(previous)
    state = SessionViewState()
    state.context_cache_ledger = ledger.as_dict()

    panels = build_panels(state, permission_mode="default")
    context = next(panel for panel in panels if panel.id == "context")
    item = next(item for item in context.items if item.id == "context-ledger")

    assert item.label.startswith("ledger · exact")
    assert "history_digest_changed" in item.detail
    assert "SYSTEM" not in item.label + item.detail
