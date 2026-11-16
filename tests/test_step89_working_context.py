"""Step 89: bounded Working Context and deterministic tool microcompaction."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

from krelcode.agent.compression import ContextManager
from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.agent.tokens import estimate_history_tokens
from krelcode.agent.tool_microcompact import (
    COMPACTED_PREFIX,
    ToolMicrocompactor,
)
from krelcode.agent.working_context import (
    WorkingContextSource,
    WorkingContextStore,
)
from krelcode.core.events import ContextCompressed, ContextMicrocompacted
from krelcode.core.messages import Message, TextBlock, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.llm.base import PreparedLLMRequest, StreamFinished, TextDelta
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.codec import history_from_dicts, history_to_dicts
from krelcode.session.state import SessionEventReducer, SessionViewState
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


def _request(tmp_path, **kwargs):
    values = {
        "turn_id": "turn-89",
        "step": 1,
        "history": (),
        "workspace_root": tmp_path,
    }
    values.update(kwargs)
    return ContextRequest(**values)


def _tool_history(size: int = 2_000, count: int = 8):
    history = [Message.system("SYS")]
    for index in range(count):
        call_id = f"c{index}"
        name = "read_file" if index % 2 == 0 else "grep"
        history.append(Message.user(f"q{index}"))
        history.append(Message.assistant(
            tool_calls=[ToolCall(call_id, name, {"path": f"f{index}.py"})]))
        history.append(Message.tool_result(
            call_id,
            f"result-{index}\n" + ("x" * size),
            blocks=[TextBlock("x" * size)],
            tool_name=name,
            is_error=False,
            metadata={
                "tool": name,
                "path": f"f{index}.py",
                "count": index,
                "turn_id": f"old-turn-{index}",
            },
        ))
    return history


def test_working_context_source_is_volatile_and_snapshot_is_bounded(tmp_path):
    store = WorkingContextStore(
        tmp_path, max_entries=4, max_entry_chars=120,
        max_chars=420, max_tokens=160)
    store.begin_turn("turn-89", "实现 Step 89")
    store.record_plan(
        ["读取代码", "实现模块", "运行测试"],
        ["completed", "in_progress", "pending"],
        active_index=1,
    )
    store.upsert("observation", "L" * 800, id="obs:long")
    store.upsert("decision", "采用确定性微压缩", id="decision:one")
    store.upsert("observation", "extra observation 1", id="obs:extra-1")
    store.upsert("observation", "extra observation 2", id="obs:extra-2")

    snapshot = store.snapshot()
    source = WorkingContextSource(store)
    assembler = ContextAssembler([source])
    assembly = asyncio.run(assembler.assemble(_request(
        tmp_path, working_context=snapshot)))

    assert snapshot.count <= 4
    assert snapshot.token_count <= 160
    assert snapshot.truncated
    assert "<working_context>" in snapshot.text
    assert "<goal>" in snapshot.text and "<plan>" in snapshot.text
    assert assembly.layers[0].cacheable is False
    assert assembly.stable_prefix_chars == 0
    assert assembly.token_contributions[0].source == "runtime:working-context"


def test_working_context_dedupes_files_and_redacts_all_entry_surfaces(tmp_path):
    redactor = SecretRedactor(["sk-step89-secret"])
    store = WorkingContextStore(tmp_path, redactor=redactor.redact)
    store.begin_turn("turn", "修复 sk-step89-secret 泄漏")
    store.upsert(
        "file",
        "已编辑 a.py",
        id="file:a.py",
        metadata={"path": "a.py", "token": "sk-step89-secret"},
    )
    store.upsert(
        "file",
        "已再次编辑 a.py",
        id="file:a.py",
        metadata={"path": "a.py", "bytes": 42},
    )
    store.record_decision("使用 sk-step89-secret", id="decision:secret")

    snapshot = store.snapshot()

    assert [entry.content for entry in snapshot.entries if entry.kind == "file"] == [
        "已再次编辑 a.py"]
    rendered = snapshot.text + json.dumps(
        [entry.metadata for entry in snapshot.entries], ensure_ascii=False)
    assert "sk-step89-secret" not in rendered
    assert "***" in rendered
    assert snapshot.as_record()["entries"] == snapshot.count
    assert snapshot.as_record()["tokens"] == snapshot.token_count


def test_working_context_has_a_secret_fallback_without_explicit_redactor(tmp_path):
    store = WorkingContextStore(tmp_path)
    store.upsert(
        "observation",
        "request failed: api_key=sk-without-redactor",
        metadata={"authorization": "token=plain-secret"},
    )

    snapshot = store.snapshot()

    assert "sk-without-redactor" not in snapshot.text
    assert "plain-secret" not in json.dumps(
        [entry.metadata for entry in snapshot.entries], ensure_ascii=False)


def test_working_context_projects_structured_tool_facts(tmp_path):
    store = WorkingContextStore(tmp_path)
    store.record_tool_result(
        ToolCall("plan-1", "update_plan", {}),
        ToolResult(
            "计划已更新",
            data={
                "items": ["A", "B"],
                "statuses": ["completed", "in_progress"],
                "active_index": 1,
                "explanation": "continue",
            },
        ),
        turn_id="turn",
    )
    store.record_tool_result(
        ToolCall("write-1", "write_file", {"path": str(tmp_path / "a.py")}),
        ToolResult("written", data={"path": str(tmp_path / "a.py"), "bytes": 12}),
        turn_id="turn",
    )
    store.record_tool_result(
        ToolCall("read-1", "read_file", {"path": str(tmp_path / "a.py")}),
        ToolResult("1→x", data={"path": str(tmp_path / "a.py"), "total_lines": 1}),
        turn_id="turn",
    )
    store.record_tool_result(
        ToolCall("cmd-1", "run_command", {"command": "pytest -q"}),
        ToolResult("exit_code: 1", is_error=True, data={"exit_code": 1}),
        turn_id="turn",
    )

    snapshot = store.snapshot()

    assert snapshot.counts_by_kind["plan"] == 1
    assert snapshot.counts_by_kind["file"] == 1
    assert snapshot.counts_by_kind["observation"] == 1
    assert snapshot.counts_by_kind["error"] == 1
    assert "a.py" in snapshot.text
    assert "exit_code: 1" in snapshot.text


def test_microcompact_protects_recent_errors_writes_and_plans():
    history = _tool_history(count=12)
    protected_specs = [
        ("err", "run_command", True, {"command": "false", "exit_code": 1}),
        ("write", "write_file", False, {"path": "keep.py", "bytes": 10}),
        ("plan", "update_plan", False, {"items": ["A"]}),
    ]
    for index, (call_id, name, is_error, meta) in enumerate(protected_specs):
        history += [
            Message.user(f"protected-{index}"),
            Message.assistant(tool_calls=[ToolCall(call_id, name, {})]),
            Message.tool_result(
                call_id,
                "Z" * 2_000,
                blocks=[TextBlock("Z" * 2_000)],
                tool_name=name,
                is_error=is_error,
                metadata={**meta, "tool": name, "turn_id": f"old-{call_id}"},
            ),
        ]
    compactor = ToolMicrocompactor(keep_recent=3)
    roles_before = [message.role for message in history]

    report = compactor.compact(history, trigger="auto")
    by_id = {
        message.tool_call_id: message for message in history if message.role == "tool"
    }

    assert report.compacted_count > 0
    assert by_id["err"].content == "Z" * 2_000
    assert by_id["write"].content == "Z" * 2_000
    assert by_id["plan"].content == "Z" * 2_000
    assert by_id["c0"].content.startswith(COMPACTED_PREFIX)
    assert [message.role for message in history] == roles_before
    assert by_id["c0"].tool_blocks is None


def test_microcompact_is_idempotent_preserves_recovery_reference_and_metadata():
    history = [Message.system("SYS")]
    for index in range(7):
        call_id = f"r{index}"
        content = (
            f"line {index}\n" + "x" * 1_000
            + f"\n[输出过长，全文已存盘: /tmp/tool-{index}.txt]"
        )
        history += [
            Message.assistant(tool_calls=[ToolCall(
                call_id, "run_command", {"command": f"cmd-{index}"})]),
            Message.tool_result(
                call_id,
                content,
                tool_name="run_command",
                is_error=False,
                metadata={
                    "tool": "run_command",
                    "command": f"cmd-{index}",
                    "exit_code": index,
                    "files": ["/tmp/" + ("f" * 400)] * 24,
                    "turn_id": "old",
                },
            ),
        ]
    compactor = ToolMicrocompactor(keep_recent=2)

    first = compactor.compact(history)
    second = compactor.compact(history)

    assert first.changed and first.summarized_count >= 1
    assert first.referenced_count >= 1
    assert not second.changed
    compacted = [m.content for m in history if m.role == "tool"][:-2]
    assert all(text.startswith(COMPACTED_PREFIX) for text in compacted)
    assert any("全文已存盘: /tmp/tool-" in text for text in compacted)
    assert any("exit=" in text for text in compacted)
    assert all(len(text) < 1_000 for text in compacted)
    assert len(history) == 1 + 7 * 2


def test_microcompact_protects_current_turn_even_outside_recent_window():
    history = _tool_history(count=10)
    for message in history:
        if message.role == "tool":
            message.tool_meta = {
                **(message.tool_meta or {}), "turn_id": "current-turn"}
    compactor = ToolMicrocompactor(keep_recent=2)

    report = compactor.compact(history, current_turn_id="current-turn")

    assert not report.changed
    assert report.protected_count == 8


def test_context_manager_microcompacts_without_llm_and_marks_preview_safe():
    history = _tool_history(size=5_000, count=10)
    provider = MockProvider(scripted=["must-not-be-used"])
    manager = ContextManager(provider, max_tokens=6_000, keep_recent_tools=3)

    preview = manager.preview_compact(history, force=False)
    assert preview.evicted_tools > 0
    assert preview.evicted_tools == 0 or manager.last_microcompact_report is None

    changed = asyncio.run(manager.maybe_compress(history))
    assert changed
    assert provider.calls == []
    assert manager.last_microcompact_report is not None
    assert manager.last_microcompact_report.compacted_count > 0
    assert not manager.over_budget(history)


def test_microcompact_event_reducer_stream_json_and_tui_are_content_free():
    event = ContextMicrocompacted(
        trigger="auto",
        before_tokens=900,
        after_tokens=300,
        compacted_count=3,
        preserved_count=2,
        protected_count=1,
        summarized_count=2,
        referenced_count=1,
    )
    query_event = QueryEvent("turn-89", 4, event)
    state = SessionViewState()
    state.begin_turn()
    SessionEventReducer().apply(query_event, state)

    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="s89", cwd="/repo", provider="mock", model="m"))
    encoded = encoder.encode(query_event)

    assert state.microcompact_count == 3
    assert state.microcompact_saved_tokens == 600
    assert encoded["type"] == "context"
    assert encoded["subtype"] == "microcompacted"
    assert encoded["compacted_count"] == 3
    assert "result" not in json.dumps(encoded)


def test_tool_metadata_survives_session_codec_roundtrip():
    message = Message.tool_result(
        "call-1",
        "done",
        tool_name="write_file",
        is_error=False,
        metadata={"path": "a.py", "bytes": 12, "turn_id": "t1"},
    )

    restored = history_from_dicts(history_to_dicts([message]))[0]

    assert restored.tool_name == "write_file"
    assert restored.tool_is_error is False
    assert restored.tool_meta == {
        "path": "a.py", "bytes": 12, "turn_id": "t1"}


class ExactBudgetProvider:
    name = "exact-budget"
    model = "test-model"
    capabilities = MockProvider().capabilities

    def __init__(self, overhead: int = 80):
        self.overhead = overhead
        self.prepared_requests = []
        self.executed = []

    def prepare_request(self, messages, tools=None):
        tokens = estimate_history_tokens(messages) + self.overhead
        prepared = PreparedLLMRequest(
            provider=self.name,
            model=self.model,
            url="https://example.invalid/v1",
            headers={},
            body=json.dumps({"tokens": tokens}).encode(),
            request_body_tokens=tokens,
            payload_digest=f"digest-{len(self.prepared_requests)}",
            output_limit_tokens=100,
            cache_mode="none",
        )
        self.prepared_requests.append(prepared)
        return prepared

    async def stream_prepared(self, prepared):
        self.executed.append(prepared)
        yield TextDelta("done")
        yield StreamFinished(Message.assistant("done"), "stop", {})

    async def stream(self, messages, tools=None):
        prepared = self.prepare_request(messages, tools)
        async for event in self.stream_prepared(prepared):
            yield event


def test_real_budget_path_microcompacts_reassembles_and_skips_llm_summary(tmp_path):
    provider = ExactBudgetProvider(overhead=80)
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
    )
    loop.history = _tool_history(size=8_000, count=8)
    loop.working_context_store.rebuild_from_history(loop.history)
    before = estimate_history_tokens(loop.history)
    probe = deepcopy(loop.history)
    ToolMicrocompactor(keep_recent=2).compact(probe, current_turn_id="")
    after_micro = estimate_history_tokens(probe)
    budget = (before + after_micro) // 2 + 80
    loop.context_manager = ContextManager(
        provider, max_tokens=budget, keep_recent_tools=2,
        keep_recent=4,
    )
    loop.tool_microcompactor = loop.context_manager.microcompactor
    events = []
    loop.bus.subscribe(events.append)

    answer = asyncio.run(loop.run("finish", turn_id="turn-budget"))

    assert answer == "done"
    assert len(provider.executed) == 1
    assert len(provider.prepared_requests) == 2
    assert len([e for e in events if isinstance(e, ContextMicrocompacted)]) == 1
    assert not [e for e in events if isinstance(e, ContextCompressed)]
    assert loop.last_context_snapshot is not None
    assert loop.last_context_snapshot.input_fits
    assert loop.last_context_snapshot.runtime_tokens > 0
    assert "runtime:working-context" in loop.last_context_snapshot.source_tokens


class CountingCompactor(ToolMicrocompactor):
    def __init__(self):
        super().__init__(keep_recent=2)
        self.calls = 0

    def compact(self, history, **kwargs):
        self.calls += 1
        return super().compact(history, **kwargs)


def test_real_budget_path_does_not_rescan_tools_before_llm_summary(tmp_path):
    provider = ExactBudgetProvider(overhead=500)
    manager = ContextManager(provider, max_tokens=100, keep_recent_tools=2)
    compactor = CountingCompactor()
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
        context_manager=manager,
        tool_microcompactor=compactor,
    )
    loop.history = _tool_history(size=1, count=8)
    events = []
    loop.bus.subscribe(events.append)

    assert asyncio.run(loop.run("finish", turn_id="turn-summary")) == "done"

    assert compactor.calls == 1
    assert len([event for event in events
                if isinstance(event, ContextCompressed)]) == 1


class MetadataTool:
    name = "read_file"
    description = "Return a structured observation."
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult(
            "observe sk-step89-secret",
            data={
                "path": "a.py",
                "total_lines": 3,
                "command": "echo sk-step89-secret",
            },
        )


def test_agent_loop_persists_redacted_tool_facts_and_injects_working_context(tmp_path):
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[ToolCall("c1", "read_file", {})]),
        "done",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([MetadataTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
        redactor=SecretRedactor(["sk-step89-secret"]),
    )

    assert asyncio.run(loop.run("inspect a.py", turn_id="turn-tools")) == "done"

    tool_message = next(message for message in loop.history if message.role == "tool")
    assert tool_message.tool_name == "read_file"
    assert tool_message.tool_is_error is False
    assert tool_message.tool_meta["path"] == "a.py"
    assert tool_message.tool_meta["command"] == "echo ***"
    assert "sk-step89-secret" not in (tool_message.content or "")
    working = loop.working_context_store.snapshot()
    assert working.counts_by_kind["observation"] == 1
    assert "a.py" in working.text
    assert "sk-step89-secret" not in working.text
    assert "runtime:working-context" in loop.last_context_snapshot.source_tokens
