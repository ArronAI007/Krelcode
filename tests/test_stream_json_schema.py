"""Stream-json v1：统一 Schema 文档、encoder 覆盖与输入校验。"""

from __future__ import annotations

import json

import pytest

from krelcode.agent.query import QueryEvent
from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.core.events import (
    AgentEvent,
    ApprovalRequested,
    CompactCheckpointCreated,
    ContextAssembled,
    ContextCompressed,
    ContextDropped,
    ContextMicrocompacted,
    ErrorOccurred,
    HookTriggered,
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
    MemoryCandidateObserved,
    MemoryLoaded,
    MemoryWritten,
    PermissionDenied,
    PlanUpdated,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    SecurityDecisionRecorded,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
    StepStarted,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
    UserSteered,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.session.stream_json import (
    StreamJsonEncoder,
    StreamJsonMetadata,
    StreamJsonProtocolError,
    parse_input_line,
)
from krelcode.session.stream_json_schema import (
    INPUT_EVENT_SCHEMAS,
    OUTPUT_EVENT_SCHEMAS,
    STREAM_JSON_PROTOCOL_NAME,
    STREAM_JSON_PROTOCOL_VERSION,
    STREAM_JSON_SCHEMA_VERSION,
    StreamJsonSchemaError,
    stream_json_schema_document,
    validate_input_message,
    validate_stream_event,
)


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


def all_encoder_events() -> list[object]:
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    return [
        RunStarted("hello"),
        StepStarted(1),
        QueryPhaseChanged("tool", "执行测试"),
        LLMStreamDelta("answer"),
        LLMThinkingDelta("think"),
        LLMResponseReceived(
            Message.assistant("answer", tool_calls=[call]),
            {"total_tokens": 3},
        ),
        ToolCallStarted(call),
        ToolProgress("call-1", "read_file", "读取中", {"bytes": 10}),
        ToolOutputDelta("call-1", "read_file", "partial", "stdout"),
        ToolCallCompleted(call, "content", False, {"lines": 1}),
        ApprovalRequested(call, "need approval", "approval-1"),
        PermissionDenied(call, "denied"),
        SecurityDecisionRecorded(
            "call-1",
            {
                "policy_id": "local",
                "policy_version": "1",
                "resource": "tool:read_file",
                "action": "tool.execute",
                "decision": "allow",
                "reason": "",
                "approval_id": "",
                "sandbox_profile": {},
                "trace_id": "trace-1",
            },
        ),
        ContextAssembled(ContextUsageSnapshot(
            turn_id="turn-1", step=1, estimated_input_tokens=80)),
        ContextDropped(("workspace:big",), "budget"),
        ContextMicrocompacted(
            "manual", 100, 40, 2, 3, 1, 2, 0),
        ContextCompressed(
            100, 40, "structured_summary", "model", 1, 90, 50,
            (("history", 60),), "compact-1"),
        MemoryLoaded(("memory-1",), (("project", 1),), 12, "scoped_memory"),
        MemoryCandidateObserved(
            "candidate-1", "pending", "explicit_remember", 0.9, 0, "digest"),
        MemoryWritten(
            "memory-1", "added", "project", "project", 12, "manual"),
        CompactCheckpointCreated(
            "compact-1", 1, 100, 40, "digest", "structured_summary",
            "manual", (("history", 60),)),
        HookTriggered("pre_tool", True, False, "read_file", "ok", "context"),
        SubagentSpawned("agent-1", "inspect", "explorer"),
        SubagentFeedUpdated("agent-1", 2, "message", "working", {"step": 2}),
        SubagentStateChanged("agent-1", "paused", "user"),
        SubagentFinished("agent-1", "completed", "done"),
        UserSteered("补充"),
        PlanUpdated(("读取", "测试"), 1, ("completed", "in_progress"), "继续"),
        UserInputRequested("input-1", "继续吗？", ("是", "否")),
        UserInputResolved("input-1", True),
        RunFinished("finished"),
        RunCancelled("stopped"),
        ErrorOccurred("boom", "provider"),
        AgentEvent(),
    ]


def test_schema_document_is_serializable_and_versioned():
    document = stream_json_schema_document()
    encoded = json.dumps(document, ensure_ascii=False)

    assert STREAM_JSON_PROTOCOL_NAME == "krelcode.stream-json"
    assert STREAM_JSON_PROTOCOL_VERSION == "1"
    assert STREAM_JSON_SCHEMA_VERSION == "1"
    assert document["x-krelcode-protocol"] == STREAM_JSON_PROTOCOL_NAME
    assert document["x-krelcode-protocol-version"] == STREAM_JSON_PROTOCOL_VERSION
    assert document["x-krelcode-schema-version"] == STREAM_JSON_SCHEMA_VERSION
    assert len(document["oneOf"]) == len(OUTPUT_EVENT_SCHEMAS) + 1
    assert len(document["$defs"]["input"]) == len(INPUT_EVENT_SCHEMAS)
    assert '"$id":"https://krelcode.dev/schemas/stream-json-v1.json"' not in encoded


def test_every_encoder_family_conforms_to_output_schema():
    encoder = StreamJsonEncoder(metadata())
    messages = [
        encoder.init_message(),
        encoder.session_end_message("control_end"),
        *[
            encoder.encode(
                envelope(event, sequence),
                result_extra={"usage": {"calls": 1}},
                extra_fields={"request_id": "request-1"},
            )
            for sequence, event in enumerate(all_encoder_events())
        ],
        encoder.job_event_message(ShellJobStarted("job-1", "watch", 42, "/repo")),
        encoder.job_event_message(ShellJobOutput("job-1", 3, "line\n")),
        encoder.job_event_message(
            ShellJobFinished("job-1", "watch", "completed", 0, "line")),
        encoder.result_message(
            subtype="success", result="ok", is_error=False,
            turn_id="turn-1", sequence=10),
        encoder.result_message(
            subtype="cancelled", result="stopped", is_error=True,
            turn_id="turn-1", sequence=11),
        encoder.result_message(
            subtype="error", result="boom", is_error=True,
            turn_id="turn-1", sequence=12),
    ]

    for message in messages:
        validate_stream_event(message)


def test_output_schema_rejects_unknown_missing_and_wrong_typed_events():
    with pytest.raises(StreamJsonSchemaError, match="未知 stream event"):
        validate_stream_event({
            "type": "assistant", "subtype": "mystery",
            "turn_id": "turn-1", "sequence": 0, "session_id": "session-1",
        })

    with pytest.raises(StreamJsonSchemaError, match="缺少字段"):
        validate_stream_event({
            "type": "tool", "subtype": "completed", "tool_call": {},
            "content": "x",
            "turn_id": "turn-1", "sequence": 0, "session_id": "session-1",
        })

    with pytest.raises(StreamJsonSchemaError, match="is_error"):
        validate_stream_event({
            "type": "result", "subtype": "success", "is_error": "false",
            "result": "ok", "turn_id": "turn-1", "session_id": "session-1",
        })

    with pytest.raises(StreamJsonSchemaError, match="protocol"):
        validate_stream_event({
            "type": "system", "subtype": "init",
            "protocol": "other", "protocol_version": "1",
            "session_id": None, "cwd": "/repo", "provider": "mock",
            "model": "mock-model", "mode": "stream-json",
            "permission_mode": "default", "tools": [], "mcp_servers": [],
        })

    generic = {
        "type": "event", "subtype": "CustomEvent",
        "turn_id": "turn-1", "sequence": 0, "session_id": "session-1",
    }
    validate_stream_event(generic)


@pytest.mark.parametrize("message", [
    {"type": "user", "content": "hello"},
    {"type": "user", "message": {"role": "user", "content": "hello"}},
    {"type": "control", "subtype": "end"},
    {"type": "control", "subtype": "interrupt"},
    {
        "type": "control", "subtype": "permission_response",
        "request_id": "call-1", "decision": "allow",
    },
    {
        "type": "control", "subtype": "user_input_response",
        "request_id": "input-1", "action": "accept", "content": "A",
    },
])
def test_input_schema_accepts_all_protocol_envelopes(message):
    validate_input_message(message)


@pytest.mark.parametrize("message, error", [
    ({"type": "assistant"}, "type 必须"),
    ({"type": "user"}, "content 或 message"),
    ({"type": "user", "content": 1}, "content 必须"),
    ({"type": "user", "message": "x"}, "message 必须是对象"),
    ({"type": "user", "message": {"role": "assistant", "content": "x"}},
     "role 必须"),
    ({"type": "control", "subtype": "pause"}, "未知 control.subtype"),
    ({"type": "control", "subtype": "permission_response",
      "decision": "allow"}, "缺少字段"),
    ({"type": "control", "subtype": "permission_response",
      "request_id": 1, "decision": "allow"}, "request_id"),
    ({"type": "control", "subtype": "user_input_response",
      "request_id": "input-1"}, "缺少字段"),
])
def test_input_schema_rejects_invalid_envelopes(message, error):
    with pytest.raises(StreamJsonSchemaError, match=error):
        validate_input_message(message)


def test_parser_uses_schema_without_changing_successful_shapes():
    rich = parse_input_line(json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "hello"},
    }))
    assert rich.content == "hello"

    with pytest.raises(StreamJsonProtocolError, match="request_id.*schema"):
        parse_input_line(json.dumps({
            "type": "control",
            "subtype": "permission_response",
            "request_id": 1,
            "decision": "allow",
        }))
