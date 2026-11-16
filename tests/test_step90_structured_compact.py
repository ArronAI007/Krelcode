"""Step 90: Structured Compact v2, persistent handoff and rewind recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.compact_summary import (
    COMPACT_SUMMARY_JSON_SCHEMA,
    CompactSummary,
    CompactSummaryError,
    merge_summaries,
)
from krelcode.agent.compression import ContextManager
from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.working_context import WorkingContextStore
from krelcode.core.errors import LLMError
from krelcode.core.events import (
    ContextCompressed,
    EventBus,
    RunFinished,
    RunStarted,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.compact_checkpoint import CompactCheckpointStore
from krelcode.session.controller import SessionController
from krelcode.session.recorder import SessionRecorder
from krelcode.session.state import SessionEventReducer, SessionViewState
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def summary_payload(
    *,
    goal: str = "完成 Structured Compact v2",
    decisions: list[str] | None = None,
    files: list[dict] | None = None,
    tests: list[dict] | None = None,
    next_steps: list[str] | None = None,
) -> dict:
    return {
        "version": 2,
        "goal": goal,
        "decisions": decisions or [],
        "files": files or [
            {"path": "src/a.py", "status": "modified", "note": "main change"},
        ],
        "commands": ["python3 -m pytest -q tests/test_step90_structured_compact.py"],
        "tests": tests or [
            {"name": "pytest step90", "status": "passed", "detail": "exit 0"},
        ],
        "errors": [],
        "open_issues": [],
        "next_steps": next_steps or ["run the full test suite"],
        "handoff_notes": ["keep the compact checkpoint"],
    }


def model_json(**kwargs) -> str:
    return json.dumps(summary_payload(**kwargs), ensure_ascii=False)


def turn_history(count: int = 8, size: int = 600) -> list[Message]:
    history = [Message.system("SYS")]
    for index in range(count):
        history.append(Message.user(f"goal-{index} " + "u" * size))
        history.append(Message.assistant(f"result-{index} " + "a" * size))
    return history


def test_compact_summary_schema_and_strict_model_parser():
    assert COMPACT_SUMMARY_JSON_SCHEMA["required"] == [
        "version", "goal", "decisions", "files", "commands", "tests",
        "errors", "open_issues", "next_steps", "handoff_notes",
    ]
    parsed = CompactSummary.from_model_json_text(model_json())
    assert parsed.goal == "完成 Structured Compact v2"
    assert parsed.files[0].path == "src/a.py"
    assert parsed.tests[0].status == "passed"
    assert parsed.validate() == []

    payload = summary_payload()
    payload.pop("tests")
    try:
        CompactSummary.from_model_json_text(json.dumps(payload))
    except CompactSummaryError as exc:
        assert "tests" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing required field must fail")

    invalid = summary_payload()
    invalid["files"][0]["status"] = "invented"
    try:
        CompactSummary.from_model_json_text(json.dumps(invalid))
    except CompactSummaryError as exc:
        assert "status" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid file status must fail")

    invalid_test = summary_payload()
    invalid_test["tests"][0]["status"] = "maybe"
    try:
        CompactSummary.from_model_json_text(json.dumps(invalid_test))
    except CompactSummaryError as exc:
        assert "status" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid test status must fail")

    oversized = summary_payload()
    oversized["files"] = [
        {"path": f"f{index}.py", "status": "read", "note": ""}
        for index in range(33)
    ]
    try:
        CompactSummary.from_model_json_text(json.dumps(oversized))
    except CompactSummaryError as exc:
        assert "files" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("oversized arrays must fail")


def test_model_summary_is_structural_and_merges_previous_handoff():
    first = CompactSummary.from_model_json_text(model_json())
    second = CompactSummary.from_model_json_text(model_json(
        goal="继续 Step 90",
        files=[],
        tests=[],
        decisions=["model output wins for changed keys"],
    ))
    merged = merge_summaries(first, second)

    assert merged.goal == "继续 Step 90"
    assert merged.files[0].path == "src/a.py"
    assert merged.tests[0].status == "passed"
    assert "model output wins for changed keys" in merged.decisions
    assert "完成 Structured Compact v2" not in merged.goal
    assert "路径" not in merged.render()  # wire aliases keep compact bounded


def test_failed_model_summary_uses_working_context_fallback(tmp_path):
    class FailingProvider:
        name = "failing"
        model = "failing"

        async def stream(self, messages, tools=None):
            raise LLMError("summary unavailable")
            yield  # pragma: no cover

    store = WorkingContextStore(tmp_path)
    store.begin_turn("turn-90", "实现 Step 90")
    store.upsert(
        "file", "修改 src/a.py", id="file:src/a.py",
        metadata={"path": "src/a.py", "action": "modified"})
    store.upsert(
        "command", "pytest -q", id="command:test",
        metadata={"command": "pytest -q", "exit_code": 0})
    manager = ContextManager(
        FailingProvider(), max_tokens=1, keep_recent=2,
        working_context_provider=store.snapshot,
    )
    history = turn_history(count=4)

    assert asyncio.run(manager.maybe_compress(history, force=True))

    summary = manager.last_summary
    assert summary is not None
    assert summary.source == "working_context"
    assert summary.files[0].path == "src/a.py"
    assert summary.tests[0].status == "passed"
    assert "摘要失败" in history[1].content


def test_three_consecutive_compacts_keep_goal_files_and_tests():
    provider = MockProvider(scripted=[
        model_json(),
        model_json(
            goal="继续 Structured Compact v2",
            files=[],
            tests=[],
            decisions=["第二个持久决策"],
        ),
        model_json(
            goal="继续 Structured Compact v2",
            files=[],
            tests=[],
            decisions=["第三个持久决策"],
            next_steps=["执行全量测试"],
        ),
    ])
    manager = ContextManager(provider, max_tokens=10**9, keep_recent=2)
    history = [Message.system("SYS")]
    for index in range(4):
        history.append(Message.user(f"q{index}"))
        history.append(Message.assistant(f"a{index}"))

    for index in range(3):
        history.append(Message.user(f"new-{index}a"))
        history.append(Message.assistant(f"done-{index}a"))
        history.append(Message.user(f"new-{index}b"))
        history.append(Message.assistant(f"done-{index}b"))
        assert asyncio.run(manager.maybe_compress(history, force=True))

    summary = manager.last_summary
    assert summary is not None
    assert summary.generation == 3
    assert summary.goal == "继续 Structured Compact v2"
    assert any(item.path == "src/a.py" for item in summary.files)
    assert any(item.status == "passed" for item in summary.tests)
    assert "第二个持久决策" in summary.decisions
    assert "第三个持久决策" in summary.decisions
    assert summary.compacted_messages > 0
    assert len(provider.calls) == 3


def test_current_turn_is_never_compacted_before_its_user_start():
    history = [Message.system("SYS")]
    for index in range(7):
        history.append(Message.user(f"old-{index}"))
        history.append(Message.assistant(f"old-answer-{index}"))
    current_index = len(history)
    history.append(Message.user("CURRENT TURN GOAL"))
    history.append(Message.assistant(
        tool_calls=[ToolCall("current-call", "read_file", {"path": "a.py"})]))
    history.append(Message.tool_result(
        "current-call", "current result",
        tool_name="read_file", metadata={"turn_id": "turn-current"}))
    history.append(Message.user("steering message inside current turn"))
    history.append(Message.assistant("current result ready"))

    manager = ContextManager(MockProvider(), max_tokens=10, keep_recent=2)
    preview = manager.preview_compact(
        history, force=True, current_turn_id="turn-current")

    assert preview.boundary is not None
    assert preview.boundary.turn_aligned
    assert preview.boundary.middle_end == current_index
    assert history[preview.boundary.tail_start].content == "CURRENT TURN GOAL"


def test_auto_compact_does_not_resummarize_only_the_previous_summary():
    previous = CompactSummary.from_model_json_text(model_json())
    history = [
        Message.system("SYS"),
        Message.user(
            previous.render(),
            images=None,
        ),
        Message.user("CURRENT TURN"),
        Message.assistant(
            tool_calls=[ToolCall("c1", "read_file", {"path": "a.py"})]),
        Message.tool_result(
            "c1", "same output", tool_name="read_file",
            metadata={"turn_id": "turn-current"}),
        Message.assistant("done"),
        Message.assistant("still complete"),
    ]
    manager = ContextManager(MockProvider(), max_tokens=10, keep_recent=2)

    preview = manager.preview_compact(
        history, force=False, current_turn_id="turn-current")

    assert not preview.eligible
    assert "尚无新的已完成内容" in preview.reason
    assert not asyncio.run(manager.maybe_compress(
        history, current_turn_id="turn-current"))


def test_low_watermark_selects_an_aggressive_safe_turn_boundary():
    history = turn_history(count=18, size=500)
    manager = ContextManager(
        MockProvider(), max_tokens=2_000, keep_recent=2,
        low_watermark_ratio=0.5)

    preview = manager.preview_compact(history, force=True)

    assert preview.eligible
    assert preview.boundary is not None
    assert preview.boundary.turn_aligned
    assert preview.after_tokens <= preview.low_watermark_tokens
    assert preview.low_watermark_reached
    assert "target reached" in preview.render(include_messages=False)


def test_preview_reports_source_savings_without_mutating_history():
    history = [Message.system("SYS")]
    for index in range(5):
        call_id = f"c{index}"
        history.append(Message.user(f"q{index}"))
        history.append(Message.assistant(
            tool_calls=[ToolCall(call_id, "grep", {"pattern": "x"})]))
        history.append(Message.tool_result(
            call_id, "X" * 2_000, tool_name="grep",
            metadata={"tool": "grep", "turn_id": f"old-{index}"}))
    history.append(Message.user("current"))
    before = [message.content for message in history]
    manager = ContextManager(
        MockProvider(), max_tokens=10_000, keep_recent=2,
        keep_recent_tools=1)

    preview = manager.preview_compact(
        history, force=True, current_turn_id="turn-current")

    assert [message.content for message in history] == before
    savings = dict(preview.source_savings)
    assert savings["tool_results"] > 0
    assert savings["conversation"] > 0
    assert savings["total"] > 0
    assert "source savings:" in preview.render(include_messages=False)


def test_compact_checkpoint_round_trip_is_redacted_and_matchable(tmp_path):
    redactor = SecretRedactor(["sk-step90-checkpoint-secret"])
    store = CompactCheckpointStore(tmp_path, redactor=redactor)
    summary = CompactSummary.from_model_json_text(model_json())
    pre_compact = [
        Message.system("SYS"),
        Message.user("token sk-step90-checkpoint-secret"),
        Message.assistant("ok"),
    ]

    checkpoint = store.record(
        pre_compact_history=pre_compact,
        summary=summary,
        trigger="auto",
        before_tokens=100,
        after_tokens=20,
        session_id="session-90",
        turn_id="turn-90",
        source_savings={"tool_results": 30, "conversation": 50, "total": 80},
        created_at=100.0,
    )

    assert checkpoint is not None
    raw = (tmp_path / ".krelcode" / "compact-checkpoints" /
           f"{checkpoint.id}.json").read_text(encoding="utf-8")
    assert "sk-step90-checkpoint-secret" not in raw
    assert "***" in raw
    loaded = store.load(checkpoint.id)
    assert loaded is not None
    assert loaded.summary.digest == summary.digest
    assert loaded.pre_compact_history[1].content.endswith("***")
    assert loaded.source_savings["total"] == 80
    assert store.find_before(
        session_id="session-90", created_at=101.0).id == checkpoint.id
    assert store.find_matching_before(
        [Message.user(summary.render())],
        session_id="session-90",
        created_at=101.0,
    ).id == checkpoint.id


def test_agent_loop_persists_checkpoint_and_emits_auditable_metadata(tmp_path):
    provider = MockProvider(scripted=[model_json()])
    bus = EventBus()
    store = CompactCheckpointStore(tmp_path)
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
        context_manager=ContextManager(
            provider, max_tokens=10**9, keep_recent=2),
        compact_checkpoint_store=store,
    )
    loop.history.extend(turn_history(count=5, size=300)[1:])
    events = []
    bus.subscribe(events.append)

    assert asyncio.run(loop.compress_context(force=True))

    event = next(e for e in events if isinstance(e, ContextCompressed))
    checkpoint = loop.last_compact_checkpoint
    assert checkpoint is not None
    assert event.checkpoint_id == checkpoint.id
    assert event.generation == 1
    assert event.summary_source == "model"
    assert dict(event.source_savings)
    assert store.load(checkpoint.id) is not None
    assert checkpoint.pre_compact_history


def test_rewind_restores_pre_compact_history_from_checkpoint(tmp_path):
    bus = EventBus()
    provider = MockProvider()
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
        context_manager=ContextManager(provider, max_tokens=10_000),
    )
    loop.compact_checkpoint_store = CompactCheckpointStore(tmp_path)
    controller = SessionController(AgentRuntime(loop), checkpoints=None)

    original = [
        Message.system("SYS"),
        Message.user("original goal"),
        Message.assistant("original answer"),
    ]
    loop.history = [message for message in original]

    async def first_turn():
        loop.history.append(Message.user("turn one"))
        await bus.emit(RunStarted("turn one", turn_id="turn-1"))
        loop.history.append(Message.assistant("one done"))
        await bus.emit(RunFinished("one done", turn_id="turn-1"))

    asyncio.run(first_turn())
    summary = CompactSummary.from_model_json_text(model_json())
    loop.compact_checkpoint_store.record(
        pre_compact_history=loop.history,
        summary=summary,
        trigger="auto",
        before_tokens=100,
        after_tokens=20,
        session_id="",
        turn_id="turn-1",
    )
    pre_compact = [message for message in loop.history]
    loop.history = [Message.system("SYS"), Message.user(summary.render())]

    async def second_turn():
        loop.history.append(Message.user("turn two"))
        await bus.emit(RunStarted("turn two", turn_id="turn-2"))
        loop.history.append(Message.assistant("two done"))
        await bus.emit(RunFinished("two done", turn_id="turn-2"))

    asyncio.run(second_turn())
    controller.rewind.rewind(2)

    assert [message.content for message in loop.history] == [
        message.content for message in pre_compact]
    assert loop.context_manager.last_generation == 0
    assert loop.last_compact_checkpoint is not None


def test_compact_observability_is_content_free_across_state_recorder_and_json(tmp_path):
    event = ContextCompressed(
        200,
        40,
        strategy="structured_summary",
        summary_source="working_context",
        generation=3,
        high_watermark_tokens=200,
        low_watermark_tokens=120,
        source_savings=(("tool_results", 100), ("conversation", 60),
                        ("total", 160)),
        checkpoint_id="000001-checkpoint",
    )

    state = SessionViewState()
    SessionEventReducer().apply(QueryEvent(
        turn_id="turn-90", sequence=0, event=event), state)
    assert state.compact_generation == 3
    assert state.compact_source == "working_context"
    assert state.compact_saved_tokens == 160
    assert state.compact_checkpoint_id == "000001-checkpoint"

    path = tmp_path / "session.jsonl"
    bus = EventBus()
    recorder = SessionRecorder(path, lambda: [])
    recorder.attach(bus)
    asyncio.run(bus.emit(event))
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["strategy"] == "structured_summary"
    assert record["source_savings"]["total"] == 160
    assert record["checkpoint_id"] == "000001-checkpoint"

    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="s", cwd=str(tmp_path), provider="mock", model="mock"))
    encoded = encoder.encode(QueryEvent(
        turn_id="turn-90", sequence=1, event=event))
    assert encoded["subtype"] == "compressed"
    assert encoded["generation"] == 3
    assert encoded["source_savings"]["total"] == 160
    assert "goal" not in json.dumps(encoded)


def test_summary_payload_is_redacted_before_becoming_history():
    redactor = SecretRedactor(["sk-step90-summary-secret"])
    payload = summary_payload(
        decisions=["use sk-step90-summary-secret"],
        next_steps=["never persist sk-step90-summary-secret"],
    )
    provider = MockProvider(scripted=[json.dumps(payload)])
    manager = ContextManager(
        provider, max_tokens=10**9, keep_recent=2, redactor=redactor)
    history = turn_history(count=4, size=20)

    assert asyncio.run(manager.maybe_compress(history, force=True))

    rendered = "\n".join(
        str(message.content or "") for message in history)
    assert "sk-step90-summary-secret" not in rendered
    assert "***" in rendered
    assert manager.last_summary is not None
    assert manager.last_summary.source == "model"
