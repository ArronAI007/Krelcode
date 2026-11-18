"""Step 93: inspectable context, manageable memory and stable JSON events."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from krelcode.agent.context_plan import ContextPlanSnapshot
from krelcode.agent.context_source import (
    ContextAssembly,
    ContextLayer,
    ContextTokenContribution,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.core.events import (
    CompactCheckpointCreated,
    ContextAssembled,
    ContextDropped,
    MemoryCandidateObserved,
    MemoryLoaded,
    MemoryWritten,
)
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.session.input import InputProcessor
from krelcode.session.memory import MemoryScope, MemoryStore
from krelcode.session.stream_json import (
    StreamJsonEncoder,
    StreamJsonMetadata,
)
from krelcode.session.stream_json_runner import run_stream_json_session
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.usage import UsageTracker
from krelcode.core.redact import SecretRedactor
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_loop(tmp_path: Path) -> AgentLoop:
    loop = AgentLoop(
        llm=MockProvider(scripted=["ok"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    loop.memory_store = MemoryStore(
        tmp_path, user_path=tmp_path / "user-memory.jsonl")
    return loop


def make_snapshot(
    *, stable_hash: str = "stable-a", dropped: tuple[str, ...] = ()
) -> ContextUsageSnapshot:
    return ContextUsageSnapshot(
        turn_id="turn-93",
        step=2,
        estimated_input_tokens=321,
        budget_tokens=1000,
        model_context_window=2000,
        output_reserve_tokens=1000,
        system_tokens=30,
        workspace_tokens=20,
        memory_tokens=15,
        history_tokens=200,
        runtime_tokens=10,
        overhead_tokens=6,
        stable_tokens=60,
        volatile_tokens=261,
        stable_hash=stable_hash,
        dynamic_hash="dynamic-a",
        full_hash="full-93",
        stable_system_block_count=2,
        dynamic_system_block_count=1,
        cache_control_boundary=60,
        tool_count=2,
        fragment_count=3,
        source_count=3,
        source_tokens={"system": 30, "memory": 15, "history": 200},
        dropped_items=dropped,
        cache_mode="explicit",
        cache_segment_count=2,
    )


def make_assembly() -> ContextAssembly:
    memory = ContextLayer(
        id="source:prompt",
        source="prompt",
        category="system",
        content="SYSTEM",
        priority=0,
        cacheable=True,
        token_contributions=(
            ContextTokenContribution("system", "system", 30),
            ContextTokenContribution("memory", "memory", 15),
        ),
        metadata={
            "fragment_count": 3,
            "memory_entry_ids": ["project-1"],
            "memory_scope_counts": {"project": 1},
            "memory_tokens": 15,
        },
    )
    runtime = ContextLayer(
        id="runtime:tools",
        source="tool_directory",
        category="runtime",
        content="tools",
        priority=100,
        token_contributions=(
            ContextTokenContribution("runtime:tools", "runtime", 10),
        ),
    )
    return ContextAssembly(
        text="SYSTEM\n\ntools",
        layers=(memory, runtime),
        stable_prefix_chars=6,
        stable_hash="stable-a",
        full_hash="full-93",
        token_contributions=(
            ContextTokenContribution("system", "system", 30),
            ContextTokenContribution("memory", "memory", 15),
            ContextTokenContribution("runtime:tools", "runtime", 10),
        ),
        source_count=2,
        fragment_count=3,
    )


def test_context_plan_why_sources_and_cache_are_content_free(tmp_path):
    loop = make_loop(tmp_path)
    loop.history = [Message.system("SYS"), Message.user("Q")]
    loop.previous_context_snapshot = make_snapshot(stable_hash="stable-old")
    loop.last_context_snapshot = make_snapshot(dropped=("workspace:large",))
    loop.last_context_assembly = make_assembly()
    loop.last_context_plan = ContextPlanSnapshot(
        turn_id="turn-93",
        step=2,
        stable_hash="stable-a",
        dynamic_hash="dynamic-a",
        full_hash="full-93",
        layer_ids=("source:prompt", "runtime:tools"),
        sources=("prompt", "tool_directory"),
        categories=("system", "runtime"),
        token_count=55,
        history_digest="history-digest",
        working_context_sequence=7,
    )
    controller = SessionController(loop, workspace_root=tmp_path)

    plan = "\n".join(asyncio.run(controller.execute_command("/context plan")).output)
    assert "source:prompt" in plan and "working-context sequence 7" in plan
    assert "dynamic hash dynamic-a" in plan

    why = "\n".join(asyncio.run(controller.execute_command(
        "/context why source:prompt")).output)
    assert "source prompt" in why and "memory [memory] 15" in why
    assert "SYSTEM" not in why

    sources = "\n".join(asyncio.run(controller.execute_command(
        "/context sources")).output)
    assert "source:prompt" in sources and "workspace:large" in sources

    cache = "\n".join(asyncio.run(controller.execute_command(
        "/context cache")).output)
    assert "changed: 是" in cache and "segments 2" in cache
    assert "stable blocks 2 · dynamic blocks 1" in cache
    assert "dynamic hash dynamic-a" in cache
    assert "cache_control boundary 60 chars" in cache


def test_context_plan_bounds_large_layer_and_source_lists(tmp_path):
    loop = make_loop(tmp_path)
    loop.last_context_plan = ContextPlanSnapshot(
        turn_id="turn-large",
        step=1,
        stable_hash="stable",
        full_hash="full",
        layer_ids=tuple(f"layer-{index}" for index in range(105)),
        sources=tuple(f"source-{index}" for index in range(104)),
        categories=tuple(f"category-{index}" for index in range(32)),
        token_count=999,
        history_digest="digest",
        working_context_sequence=1,
    )
    controller = SessionController(loop, workspace_root=tmp_path)

    output = "\n".join(asyncio.run(controller.execute_command(
        "/context plan")).output)
    assert "layer-99" in output and "layer-100" not in output
    assert "另有 5 个" in output
    assert "source-99" in output and "source-100" not in output
    assert "另有 4 个" in output
    assert "另有 2 个类别" in output


def test_context_plan_snapshot_round_trips_dynamic_cache_hash():
    plan = ContextPlanSnapshot(
        turn_id="turn-cache",
        step=3,
        engine="v3",
        stable_hash="stable-hash",
        dynamic_hash="dynamic-hash",
        full_hash="full-hash",
    )

    restored = ContextPlanSnapshot.from_dict(plan.as_dict())

    assert restored == plan
    assert restored.dynamic_hash == "dynamic-hash"


def test_context_snapshot_survives_turn_boundary_for_cache_comparison(tmp_path):
    loop = AgentLoop(
        llm=MockProvider(scripted=["first", "second"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )

    asyncio.run(loop.run("one", turn_id="turn-one"))
    first_turn_snapshot = loop.last_context_snapshot
    assert first_turn_snapshot is not None

    asyncio.run(loop.run("two", turn_id="turn-two"))
    assert loop.previous_context_snapshot is first_turn_snapshot
    assert loop.last_context_snapshot is not first_turn_snapshot


def test_memory_new_add_syntax_show_forget_and_scope_list(tmp_path):
    loop = make_loop(tmp_path)
    controller = SessionController(loop, workspace_root=tmp_path)

    added = asyncio.run(controller.execute_command(
        "/memory add 默认使用中文 --scope user"))
    assert "已写入用户记忆" in added.output[0]
    entry = loop.memory_store.list(scopes=(MemoryScope.USER,))[0]

    shown = "\n".join(asyncio.run(controller.execute_command(
        f"/memory show {entry.id[-6:]}")).output)
    assert entry.id in shown and "默认使用中文" in shown

    listed = "\n".join(asyncio.run(controller.execute_command(
        "/memory list user")).output)
    assert entry.id in listed and "用户记忆" in listed

    forgotten = asyncio.run(controller.execute_command(
        f"/memory forget {entry.id}"))
    assert "已删除" in forgotten.output[0]
    assert loop.memory_store.list(scopes=(MemoryScope.USER,)) == ()


def test_memory_migration_is_backed_up_deduplicated_and_idempotent(tmp_path):
    path = tmp_path / ".krelcode" / "memory" / "entries.jsonl"
    path.parent.mkdir(parents=True)
    legacy = [
        {"id": "legacy-1", "text": "测试命令是 pytest -q", "created_at": "2025-01-01"},
        {"id": "legacy-2", "text": "测试命令是 pytest -q", "created_at": "2025-01-02", "pinned": True},
    ]
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in legacy)
        + "\nnot-json\n",
        encoding="utf-8",
    )
    store = MemoryStore(tmp_path, user_path=tmp_path / "user.jsonl")
    loop = make_loop(tmp_path)
    loop.memory_store = store
    controller = SessionController(loop, workspace_root=tmp_path)

    first = "\n".join(asyncio.run(controller.execute_command(
        "/memory migrate project")).output)
    assert "已更新" in first and "backup:" in first
    entries = store.list(limit=0, scopes=(MemoryScope.PROJECT,))
    assert len(entries) == 1 and entries[0].pinned is True
    backups = list(path.parent.glob("entries.jsonl.bak-*"))
    assert len(backups) == 1

    second = "\n".join(asyncio.run(controller.execute_command(
        "/memory migrate project")).output)
    assert "无需更新" in second
    assert len(list(path.parent.glob("entries.jsonl.bak-*"))) == 1


def test_compact_focus_is_forwarded(tmp_path):
    loop = make_loop(tmp_path)
    captured = {}

    async def fake_compress_context(force=False, *, focus="", **kwargs):
        captured["force"] = force
        captured["focus"] = focus
        return False

    loop.compress_context = fake_compress_context
    loop.context_manager = object()
    controller = SessionController(loop, workspace_root=tmp_path)

    result = asyncio.run(controller.execute_command(
        "/compact 保留 API 设计和测试状态"))
    assert result.handled
    assert captured == {"force": True, "focus": "保留 API 设计和测试状态"}


def test_new_stream_events_are_namespaced_and_content_free():
    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="s93", cwd="/repo", provider="mock", model="mock",
        run_mode="stream-json"))
    events = [
        (ContextDropped(("workspace:big",)), "context_dropped", "context/dropped"),
        (MemoryLoaded(("project-1",), (("project", 1),), 12),
         "memory_loaded", "memory/loaded"),
        (MemoryCandidateObserved("candidate-1", trigger="explicit_remember",
                                 confidence=0.9, digest="digest"),
         "memory_candidate", "memory/candidate"),
        (MemoryWritten("project-1", action="added", scope="project",
                       kind="note", tokens=4),
         "memory_written", "memory/written"),
        (CompactCheckpointCreated("000001-checkpoint", generation=2,
                                  before_tokens=100, after_tokens=50,
                                  summary_digest="summary-digest"),
         "compact_checkpoint", "compact/checkpoint"),
    ]
    for index, (event, name, subtype) in enumerate(events):
        payload = encoder.encode(QueryEvent("turn-93", index, event))
        assert payload["event"] == name
        assert f"{payload['type']}/{payload['subtype']}" == subtype
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "memory body" not in serialized
        assert "secret-value" not in serialized


def test_stream_json_executes_context_and_memory_commands_without_model(tmp_path):
    loop = make_loop(tmp_path)
    runtime = AgentRuntime(loop)
    usage = UsageTracker(model="mock")
    usage.attach(loop.bus)
    processor = InputProcessor(tmp_path)
    output = io.StringIO()

    async def run(prompt: str, stdin: str = ""):
        output.seek(0)
        output.truncate(0)
        code = await run_stream_json_session(
            runtime,
            session_path=None,
            input_processor=processor,
            usage=usage,
            redactor=SecretRedactor(),
            initial_prompt=prompt,
            input_stream=io.StringIO(stdin),
            output_stream=output,
        )
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    code, rows = asyncio.run(run("/memory add 项目使用 asyncio --scope project"))
    assert code == 0
    assert any(row.get("event") == "memory_written" for row in rows)
    assert any(row["type"] == "command" and row["handled"] for row in rows)
    assert rows[-2]["type"] == "result" and rows[-2]["is_error"] is False
    assert len(loop.memory_store.list()) == 1

    code, rows = asyncio.run(run("/context"))
    assert code == 0
    command = next(row for row in rows if row["type"] == "command")
    assert command["command"] == "/context"
    assert any("[context]" in line for line in command["output"])


def test_repl_panel_redraws_do_not_rescan_memory(tmp_path, monkeypatch):
    from krelcode.ui.repl import run_repl

    loop = make_loop(tmp_path)
    store = loop.memory_store
    calls = 0
    original = store.dashboard_snapshot

    def counted(*, limit=8):
        nonlocal calls
        calls += 1
        return original(limit=limit)

    store.dashboard_snapshot = counted

    class ScriptedInput:
        def __init__(self):
            self.lines = ["/context", "/exit"]

        async def __call__(self, prompt):
            await asyncio.sleep(0.01)
            return self.lines.pop(0)

    code = asyncio.run(run_repl(
        loop,
        input_fn=ScriptedInput(),
        persist_history=False,
    ))
    assert code == 0
    assert calls == 1


def test_real_request_emits_memory_loaded_ids_without_memory_text(tmp_path):
    from krelcode.agent.fragments import PromptFragmentRegistry, base_fragment

    loop = make_loop(tmp_path)
    entry = loop.memory_store.add("项目测试命令是 pytest -q")
    registry = PromptFragmentRegistry([base_fragment()])
    registry.upsert(loop.memory_store.fragment())
    loop.prompt_registry = registry
    loop.prompt_snapshot = registry.snapshot()
    loop.history = [Message.system(loop.prompt_snapshot.text)]
    events = []
    loop.bus.subscribe(events.append)

    asyncio.run(loop.run("hello", turn_id="turn-93-memory"))

    loaded = [event for event in events if isinstance(event, MemoryLoaded)]
    assert loaded
    assert entry.id in loaded[0].entry_ids
    assert dict(loaded[0].scope_counts)["project"] == 1
    assert "pytest -q" not in repr(loaded[0])
