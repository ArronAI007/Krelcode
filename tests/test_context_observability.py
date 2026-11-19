"""Step 84: context usage snapshots and observable request baselines."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.context import ContextSection, WorkspaceContext
from krelcode.agent.context_usage import (
    build_context_usage_snapshot,
    estimate_tool_schema_tokens,
)
from krelcode.agent.fragments import (
    GROUP_RUNTIME,
    PromptFragment,
    PromptFragmentRegistry,
    base_fragment,
    workspace_fragments,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.agent.tokens import estimate_history_tokens
from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.core.events import (
    ContextAssembled, EventBus, LLMStreamDelta, RunFinished,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_context
from krelcode.session.commands.types import CommandRequest
from krelcode.session.recorder import SessionRecorder
from krelcode.session.state import SessionEventReducer, SessionViewState
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.render import StreamRenderer
from krelcode.ui.tui.panels import build_panels


class EchoTool:
    name = "echo"
    description = "Echo a short message."
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult(content=str(args.get("message", "ok")))


def _workspace_bundle(tmp_path: Path) -> WorkspaceContext:
    section = ContextSection(
        "rules", "RULES SENTINEL: keep changes focused",
        priority=10, source="AGENTS.md")
    return WorkspaceContext(
        root=tmp_path,
        sections=(section,),
        text=f"<workspace_context>\n{section.content}\n</workspace_context>",
        max_tokens=1_000,
        total_tokens=section.tokens,
    )


def test_context_snapshot_partitions_exact_provider_bound_request(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(),
        *workspace_fragments(_workspace_bundle(tmp_path)),
        PromptFragment(
            id="memory:project",
            source="memory",
            title="Memory",
            content="<project_memory>\nMEMORY SENTINEL\n</project_memory>",
            group=GROUP_RUNTIME,
            cacheable=False,
        ),
    ])
    prompt_snapshot = registry.snapshot()
    hint = "<runtime_context>\nRUNTIME SENTINEL\n</runtime_context>"
    messages = [
        Message.system(prompt_snapshot.text + "\n\n" + hint),
        Message.user("inspect the request"),
        Message.assistant("ready"),
    ]
    tool_specs = [{
        "name": "read_file",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    }]

    usage = build_context_usage_snapshot(
        messages,
        turn_id="turn-84",
        step=2,
        tool_specs=tool_specs,
        prompt_snapshot=prompt_snapshot,
        base_system_text=prompt_snapshot.text,
        runtime_hints=[hint],
        budget_tokens=8_000,
        model_context_window=128_000,
    )

    schema_tokens = estimate_tool_schema_tokens(tool_specs)
    assert usage.estimated_input_tokens == (
        estimate_history_tokens(messages) + schema_tokens)
    assert sum(usage.source_token_map.values()) == usage.estimated_input_tokens
    assert usage.system_tokens > 0
    assert usage.workspace_tokens > 0
    assert usage.memory_tokens > 0
    assert usage.history_tokens > 0
    assert usage.runtime_tokens > 0
    assert usage.tool_schema_tokens > 0
    assert usage.stable_tokens == prompt_snapshot.cache.stable_tokens
    assert usage.cache_boundary_chars == prompt_snapshot.cache.offset
    assert usage.stable_hash == prompt_snapshot.cache.stable_hash
    assert usage.full_hash == prompt_snapshot.cache.full_hash
    assert usage.tool_count == 1
    assert usage.fragment_count == len(prompt_snapshot.fragments)
    assert usage.output_reserve_tokens == 120_000

    record_text = json.dumps(usage.as_record(), ensure_ascii=False)
    assert "RULES SENTINEL" not in record_text
    assert "MEMORY SENTINEL" not in record_text
    assert "RUNTIME SENTINEL" not in record_text


def test_agent_loop_emits_snapshot_before_each_model_request(tmp_path):
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[
            ToolCall("echo-1", "echo", {"message": "hello"}),
        ]),
        "finished",
    ])
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([EchoTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYSTEM SENTINEL",
    )

    assert asyncio.run(loop.run(
        "use the echo tool", turn_id="turn-events")) == "finished"

    assembled = [event for event in events if isinstance(event, ContextAssembled)]
    assert [event.snapshot.step for event in assembled] == [1, 2]
    assert all(event.snapshot.turn_id == "turn-events" for event in assembled)
    assert assembled[0].snapshot.tool_count == 1
    assert assembled[1].snapshot.history_tokens > assembled[0].snapshot.history_tokens
    assert loop.last_context_snapshot == assembled[-1].snapshot
    assert loop.last_prompt_plan is not None
    assert assembled[-1].snapshot.stable_hash == loop.last_prompt_plan.stable_hash
    assert assembled[-1].snapshot.dynamic_hash == loop.last_prompt_plan.dynamic_hash
    assert assembled[-1].snapshot.cache_boundary_chars == (
        loop.last_prompt_plan.cache_boundary)
    assert len(provider.calls) == len(assembled)
    assert "SYSTEM SENTINEL" not in json.dumps(
        assembled[-1].snapshot.as_record(), ensure_ascii=False)


def test_stream_json_and_reducer_expose_context_snapshot():
    snapshot = ContextUsageSnapshot(
        turn_id="turn-1",
        step=1,
        estimated_input_tokens=123,
        budget_tokens=1_000,
        model_context_window=2_000,
        output_reserve_tokens=1_000,
        system_tokens=20,
        tool_schema_tokens=10,
        history_tokens=80,
        runtime_tokens=10,
        overhead_tokens=3,
        stable_tokens=15,
        volatile_tokens=108,
        cache_boundary_chars=42,
        stable_hash="stable",
        full_hash="full",
        tool_count=2,
        fragment_count=3,
        source_count=2,
        source_tokens={"history": 80, "system": 20, "tool_schema": 10,
                       "runtime:turn": 10, "overhead": 3},
    )
    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="session-84",
        cwd="/repo",
        provider="mock",
        model="mock",
        run_mode="stream-json",
    ))
    encoded = encoder.encode(QueryEvent(
        turn_id="turn-1",
        sequence=7,
        event=ContextAssembled(snapshot),
    ))
    assert encoded["type"] == "context"
    assert encoded["subtype"] == "assembled"
    assert encoded["snapshot"]["estimated_input_tokens"] == 123
    assert encoded["snapshot"]["source_tokens"]["history"] == 80
    assert encoded["snapshot"]["source_count"] == 2
    assert encoded["sequence"] == 7

    chunks = []
    renderer = StreamRenderer(write=chunks.append, color=False)
    renderer(LLMStreamDelta("answer"))
    renderer(ContextAssembled(snapshot))
    renderer(RunFinished("answer"))
    assert "".join(chunks) == "⏺ answer\n"

    state = SessionViewState()
    state.begin_turn(turn_id="turn-1")
    SessionEventReducer().apply(QueryEvent(
        turn_id="turn-1", sequence=0, event=ContextAssembled(snapshot)), state)
    assert state.context_snapshot == snapshot


def test_recorder_persists_compact_snapshot_only(tmp_path):
    path = tmp_path / "session.jsonl"
    bus = EventBus()
    recorder = SessionRecorder(path, lambda: [])
    recorder.attach(bus)
    snapshot = ContextUsageSnapshot(
        turn_id="turn-1",
        step=1,
        estimated_input_tokens=50,
        system_tokens=10,
        history_tokens=40,
        full_hash="hash",
        source_tokens={"system": 10, "history": 40},
    )

    asyncio.run(bus.emit(ContextAssembled(snapshot)))

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["type"] == "context_assembled"
    assert record["estimated_input_tokens"] == 50
    assert record["source_tokens"] == {"history": 40, "system": 10}
    assert "prompt" not in record
    assert "messages" not in record


def test_context_command_reports_last_real_request_snapshot(tmp_path):
    loop = AgentLoop(
        llm=MockProvider(),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
    )
    loop.history.extend([Message.user("question"), Message.assistant("answer")])
    loop.last_context_snapshot = ContextUsageSnapshot(
        turn_id="turn-84",
        step=3,
        estimated_input_tokens=456,
        budget_tokens=1_000,
        model_context_window=2_000,
        output_reserve_tokens=1_000,
        system_tokens=20,
        tool_schema_tokens=30,
        workspace_tokens=40,
        memory_tokens=5,
        history_tokens=300,
        runtime_tokens=50,
        overhead_tokens=11,
        stable_tokens=50,
        volatile_tokens=406,
        cache_boundary_chars=123,
        stable_hash="stable-hash",
        dynamic_hash="dynamic-hash",
        full_hash="full-hash",
        stable_system_block_count=2,
        dynamic_system_block_count=1,
        cache_control_boundary=123,
        tool_count=4,
        fragment_count=6,
        source_count=3,
        source_tokens={"history": 300, "runtime:turn": 50,
                       "workspace:rules": 40, "tool_schema": 30,
                       "system": 20, "overhead": 11, "memory": 5},
    )
    ctx = CommandContext(
        request=CommandRequest(
            name="context", arguments="", raw_text="/context"),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=tmp_path,
    )

    result = asyncio.run(handle_context(ctx))
    output = "\n".join(result.output)

    assert "最近组装请求 · turn-84 · Step 3" in output
    assert "输入估算: 456 tokens" in output
    assert "tool schema 30" in output
    assert "stable 50 · volatile 406" in output
    assert "stable blocks 2 · dynamic blocks 1" in output
    assert "dynamic hash: dynamic-hash" in output
    assert "cache_control boundary: 123 chars" in output
    assert "工具 4 个 · fragment 6 个 · source 3 个" in output


def test_tui_context_panel_prefers_request_snapshot():
    state = SessionViewState()
    state.begin_turn("working")
    state.context_snapshot = ContextUsageSnapshot(
        turn_id="turn-1",
        step=1,
        estimated_input_tokens=321,
        budget_tokens=1_000,
        system_tokens=30,
        tool_schema_tokens=12,
        workspace_tokens=40,
        memory_tokens=7,
        history_tokens=200,
        runtime_tokens=20,
        overhead_tokens=12,
        stable_tokens=50,
        volatile_tokens=271,
        cache_boundary_chars=64,
        stable_system_block_count=2,
        dynamic_system_block_count=1,
        cache_control_boundary=64,
        tool_count=3,
        fragment_count=5,
        source_count=4,
    )

    panels = build_panels(
        state,
        permission_mode="default",
        context_tokens=999,
        context_budget=9_999,
        fragment_count=99,
        stable_fragments=88,
        memory_entries=(),
        memory_tokens=777,
    )
    context = {panel.id: panel for panel in panels}["context"]
    labels = " ".join(item.label for item in context.items)
    details = " ".join(item.detail for item in context.items)

    assert "context · 321/1000 tokens" in labels
    assert "system · 30" in labels
    assert "cache · 50/271" in labels
    assert "fragments · 5" in labels
    assert "workspace 40" in details
    assert "blocks 2/1" in details
    assert "control 64 chars" in details
    assert "sources 4" in details
    assert "tools 3" in details and "schema 12" in details
