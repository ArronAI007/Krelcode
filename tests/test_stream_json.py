"""stream-json v1：纯协议、CLI 校验与持久会话运行器。"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.usage import UsageTracker
from krelcode.app import build_parser
from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.core.events import (
    ApprovalRequested,
    ContextAssembled,
    ContextCompressed,
    ErrorOccurred,
    HookTriggered,
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
    PermissionDenied,
    PlanUpdated,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    StepStarted,
    SubagentFinished,
    SubagentSpawned,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserSteered,
)
from krelcode.core.messages import ImageRef, Message, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.llm.base import StreamFinished, TextDelta
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.stream_json import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    StreamControlMessage,
    StreamJsonEncoder,
    StreamJsonMetadata,
    StreamJsonProtocolError,
    StreamUserMessage,
    dumps_message,
    parse_input_line,
)
from krelcode.session.stream_json_runner import run_stream_json_session
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def metadata() -> StreamJsonMetadata:
    return StreamJsonMetadata(
        session_id="session-1",
        cwd="/repo",
        provider="mock",
        model="mock-model",
        tools=("read_file",),
        mcp_servers=("docs",),
        permission_mode="read-only",
        run_mode="stream-json",
    )


def envelope(event, sequence: int = 0) -> QueryEvent:
    return QueryEvent(turn_id="turn-1", sequence=sequence, event=event)


def test_init_and_session_end_protocol_metadata():
    encoder = StreamJsonEncoder(metadata())
    assert encoder.init_message() == {
        "type": "system",
        "subtype": "init",
        "protocol": PROTOCOL_NAME,
        "protocol_version": PROTOCOL_VERSION,
        "session_id": "session-1",
        "cwd": "/repo",
        "provider": "mock",
        "model": "mock-model",
        "mode": "stream-json",
        "permission_mode": "read-only",
        "tools": ["read_file"],
        "mcp_servers": ["docs"],
    }
    assert encoder.session_end_message("control_end") == {
        "type": "system",
        "subtype": "session_end",
        "session_id": "session-1",
        "reason": "control_end",
    }


def test_encoder_preserves_envelope_and_encodes_all_event_families():
    encoder = StreamJsonEncoder(metadata())
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    events = [
        RunStarted("hello"),
        StepStarted(1),
        LLMThinkingDelta("think"),
        LLMStreamDelta("answer"),
        LLMResponseReceived(
            Message.assistant("answer", tool_calls=[call]),
            {"total_tokens": 3},
        ),
        ToolCallStarted(call),
        ToolCallCompleted(call, "content", False, data={"lines": 1}),
        ApprovalRequested(call, "need approval"),
        PermissionDenied(call, "denied"),
        ContextCompressed(100, 40),
        ContextAssembled(ContextUsageSnapshot(
            turn_id="turn-1", step=1, estimated_input_tokens=80)),
        HookTriggered("pre_tool", True, False, "read_file", "ok", "context"),
        SubagentSpawned("agent-1", "inspect"),
        SubagentFinished("agent-1", "completed", "done"),
        UserSteered("补充"),
        RunFinished("finished"),
        RunCancelled("stopped"),
        ErrorOccurred("boom", "provider"),
    ]

    encoded = [
        encoder.encode(
            envelope(event, index),
            result_extra={"usage": {"calls": 1}},
            extra_fields={"request_id": "req-1"},
        )
        for index, event in enumerate(events)
    ]

    assert encoded[0]["type"] == "system"
    assert encoded[0]["subtype"] == "turn_started"
    assert encoded[2]["subtype"] == "thinking_delta"
    assert encoded[3]["subtype"] == "text_delta"
    assert encoded[4]["message"]["tool_calls"][0]["name"] == "read_file"
    assert encoded[5]["tool_call"]["id"] == "call-1"
    assert encoded[6]["data"] == {"lines": 1}
    assert encoded[7]["subtype"] == "requested"
    assert encoded[8]["subtype"] == "denied"
    assert encoded[9]["subtype"] == "compressed"
    assert encoded[10]["subtype"] == "assembled"
    assert encoded[10]["snapshot"]["estimated_input_tokens"] == 80
    assert encoded[11]["subtype"] == "triggered"
    assert encoded[12]["subtype"] == "started"
    assert encoded[13]["subtype"] == "finished"
    assert encoded[14]["subtype"] == "steered"
    assert encoded[15]["subtype"] == "success"
    assert encoded[16]["subtype"] == "cancelled"
    assert encoded[17]["type"] == "error"
    for sequence, item in enumerate(encoded):
        assert item["turn_id"] == "turn-1"
        assert item["sequence"] == sequence
        assert item["session_id"] == "session-1"
        assert item["request_id"] == "req-1"
    assert encoded[15]["result"] == "finished"
    assert encoded[16]["result"] == "stopped"


def test_encoder_encodes_query_progress_and_input_events():
    encoder = StreamJsonEncoder(metadata())
    events = [
        QueryPhaseChanged("tool", "执行测试"),
        ToolProgress("call-1", "run_command", "命令运行中", {"pid": 7}),
        ToolOutputDelta("call-1", "run_command", "partial", "combined"),
        PlanUpdated(("读取", "测试"), active_index=1),
        UserInputRequested("input-1", "继续吗？", ("是", "否")),
    ]

    encoded = [encoder.encode(envelope(event, index))
               for index, event in enumerate(events)]

    assert encoded[0] == {
        "type": "query",
        "subtype": "phase_changed",
        "phase": "tool",
        "detail": "执行测试",
        "turn_id": "turn-1",
        "sequence": 0,
        "session_id": "session-1",
    }
    assert encoded[1]["subtype"] == "progress"
    assert encoded[1]["data"] == {"pid": 7}
    assert encoded[2]["subtype"] == "output_delta"
    assert encoded[2]["stream"] == "combined"
    assert encoded[3]["type"] == "plan"
    assert encoded[3]["items"] == ["读取", "测试"]
    assert encoded[3]["statuses"] == []
    assert encoded[3]["explanation"] == ""
    assert encoded[4]["type"] == "input"
    assert encoded[4]["options"] == ["是", "否"]
    assert encoded[4]["allow_free_text"] is True


def test_terminal_fields_cannot_be_overwritten_by_extensions():
    encoder = StreamJsonEncoder(metadata())
    payload = encoder.encode(
        envelope(RunFinished("real"), 9),
        extra_fields={"result": "fake", "subtype": "error", "is_error": True},
    )
    assert payload["result"] == "real"
    assert payload["subtype"] == "success"
    assert payload["is_error"] is False

    manual = encoder.result_message(
        subtype="error",
        result="failure",
        is_error=True,
        turn_id="turn-9",
        sequence=10,
        extra={
            "type": "assistant",
            "subtype": "success",
            "result": "fake",
            "is_error": False,
            "turn_id": "wrong",
            "session_id": "wrong",
        },
    )
    assert manual["type"] == "result"
    assert manual["subtype"] == "error"
    assert manual["result"] == "failure"
    assert manual["is_error"] is True
    assert manual["turn_id"] == "turn-9"
    assert manual["session_id"] == "session-1"
    assert manual["sequence"] == 10


def test_parse_string_and_anthropic_style_content_blocks():
    plain = parse_input_line(
        '{"type":"user","content":"hello","request_id":"r1"}')
    assert plain == StreamUserMessage("hello", request_id="r1")

    rich = parse_input_line(json.dumps({
        "type": "user",
        "request_id": "r2",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "input_text", "text": "here"},
            ],
        },
    }))
    assert rich == StreamUserMessage("look\nhere", request_id="r2")


def test_parse_image_blocks_and_control_messages():
    parsed = parse_input_line(json.dumps({
        "type": "user",
        "content": [{
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "YWJj",
                "path": "/tmp/a.png",
            },
        }],
    }))
    assert isinstance(parsed, StreamUserMessage)
    assert parsed.content == ""
    assert parsed.images == (
        ImageRef("image/png", "YWJj", "/tmp/a.png"),
    )
    assert parse_input_line('{"type":"control","subtype":"end"}') == \
        StreamControlMessage("end")
    assert parse_input_line('{"type":"control","subtype":"interrupt"}') == \
        StreamControlMessage("interrupt")


@pytest.mark.parametrize("line, message", [
    ("not-json", "JSON 解析失败"),
    ("[]", "输入必须是 JSON 对象"),
    ('{"type":"assistant","content":"x"}', "type 必须"),
    ('{"type":"control","subtype":"pause"}', "未知 control"),
    ('{"type":"user","content":" "}', "不能为空"),
    ('{"type":"user","content":[{"type":"audio"}]}', "不支持"),
    ('{"type":"user","content":{"text":"x"}}', "必须是字符串或"),
    ('{"type":"user","message":{"role":"assistant","content":"x"}}',
     "role 必须"),
    ('{"type":"user","content":[{"type":"image","source":{}}]}',
     "缺少 media_type/data"),
])
def test_parse_rejects_invalid_input(line, message):
    with pytest.raises(StreamJsonProtocolError, match=message):
        parse_input_line(line)


def test_dumps_message_is_compact_utf8_json():
    text = dumps_message({"type": "assistant", "text": "中文"})
    assert text == '{"type":"assistant","text":"中文"}'
    assert json.loads(text)["text"] == "中文"


def make_runtime(tmp_path: Path, provider=None) -> tuple[AgentRuntime, UsageTracker]:
    loop = AgentLoop(
        llm=provider or MockProvider(scripted=["reply"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    loop.skill_manager = None
    runtime = AgentRuntime(loop)
    usage = UsageTracker(model=getattr(loop.llm, "model", None))
    usage.attach(loop.bus)
    return runtime, usage


def run_jsonl(
    tmp_path: Path,
    *,
    provider=None,
    prompt=None,
    stdin="",
    redactor=None,
):
    runtime, usage = make_runtime(tmp_path, provider)
    output = io.StringIO()
    rc = asyncio.run(run_stream_json_session(
        runtime,
        session_path=None,
        input_processor=None,
        usage=usage,
        redactor=redactor or SecretRedactor(),
        initial_prompt=prompt,
        input_stream=io.StringIO(stdin),
        output_stream=output,
    ))
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    return rc, rows


def test_runner_single_prompt_emits_one_terminal_result(tmp_path):
    rc, rows = run_jsonl(tmp_path, prompt="hello")
    assert rc == 0
    assert rows[0]["type"] == "system" and rows[0]["subtype"] == "init"
    assert rows[0]["protocol"] == PROTOCOL_NAME
    assembled = [
        item for item in rows
        if item["type"] == "context" and item["subtype"] == "assembled"
    ]
    assert assembled
    snapshot = assembled[-1]["snapshot"]
    assert "stable_hash" in snapshot
    assert snapshot["dynamic_hash"]
    assert snapshot["full_hash"]
    assert rows[-1]["type"] == "system" and rows[-1]["subtype"] == "session_end"
    results = [item for item in rows if item["type"] == "result"]
    assert len(results) == 1
    assert results[0]["subtype"] == "success"
    assert results[0]["result"] == "reply"
    assert results[0]["turn_id"] == "turn-1"


def test_runner_supports_multiple_persistent_stdin_turns(tmp_path):
    stdin = "\n".join([
        json.dumps({"type": "user", "content": "one", "request_id": "r1"}),
        json.dumps({"type": "user", "content": "two", "request_id": "r2"}),
        json.dumps({"type": "control", "subtype": "end"}),
    ]) + "\n"
    provider = MockProvider(scripted=["one reply", "two reply"])
    rc, rows = run_jsonl(tmp_path, provider=provider, stdin=stdin)
    assert rc == 0
    results = [item for item in rows if item["type"] == "result"]
    assert [(item["request_id"], item["result"]) for item in results] == [
        ("r1", "one reply"),
        ("r2", "two reply"),
    ]
    assert rows[-1]["reason"] == "control_end"


def test_runner_redacts_secret_before_serialization(tmp_path):
    secret = 'sk-"quoted-secret'
    redactor = SecretRedactor([secret])
    rc, rows = run_jsonl(
        tmp_path,
        prompt=f"use {secret}",
        redactor=redactor,
    )
    assert rc == 0
    rendered = json.dumps(rows, ensure_ascii=False)
    assert secret not in rendered
    assert "***" in rows[1]["content"]


def test_runner_protocol_error_returns_exit_code_2(tmp_path):
    rc, rows = run_jsonl(tmp_path, stdin="not json\n")
    assert rc == 2
    result = next(item for item in rows if item["type"] == "result")
    assert result["subtype"] == "error"
    assert result["is_error"] is True
    assert result["code"] == "input_error"
    assert rows[-1]["subtype"] == "session_end"


def test_runner_interrupt_cancels_turn_and_keeps_protocol_terminal(tmp_path):
    class SlowProvider(MockProvider):
        async def stream(self, messages, tools=None):
            await asyncio.sleep(10)
            yield TextDelta("late")
            yield StreamFinished(Message.assistant("late"), "stop", {})

    stdin = "\n".join([
        json.dumps({"type": "user", "content": "long", "request_id": "r1"}),
        json.dumps({"type": "control", "subtype": "interrupt"}),
    ]) + "\n"
    rc, rows = run_jsonl(
        tmp_path,
        provider=SlowProvider(),
        stdin=stdin,
    )
    assert rc == 0
    results = [item for item in rows if item["type"] == "result"]
    assert len(results) == 1
    assert results[0]["subtype"] == "cancelled"
    assert results[0]["request_id"] == "r1"
    assert rows[-1]["subtype"] == "session_end"


def test_cli_stream_json_parameter_validation():
    from krelcode.app import async_main

    invalid = [
        ["--input-format", "stream-json", "--output-format", "text"],
        ["--input-format", "stream-json", "--output-format", "stream-json",
         "-m", "hello"],
        ["--input-format", "stream-json", "--output-format", "stream-json",
         "--message", ""],
        ["--output-format", "stream-json"],
    ]
    for argv in invalid:
        args = build_parser().parse_args(argv)
        assert asyncio.run(async_main(args)) == 2


def test_stream_json_cli_is_headless_mode():
    from krelcode.app import resolve_run_mode
    from krelcode.tools.base import RunMode

    persistent = build_parser().parse_args([
        "--input-format", "stream-json",
        "--output-format", "stream-json",
    ])
    single = build_parser().parse_args([
        "-m", "hello", "--output-format", "stream-json",
    ])
    assert resolve_run_mode(persistent) is RunMode.JSON
    assert resolve_run_mode(single) is RunMode.JSON
