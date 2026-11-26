"""Machine-readable schema and validators for the stream-json v1 protocol.

The schema is intentionally dependency-free.  It is both the runtime contract
used by the encoder/parser and a serializable document that SDKs can import for
code generation or documentation.  Additional metadata remains allowed so v1
consumers can evolve without losing forward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


STREAM_JSON_PROTOCOL_NAME = "krelcode.stream-json"
STREAM_JSON_PROTOCOL_VERSION = "1"
STREAM_JSON_SCHEMA_VERSION = "1"


class StreamJsonSchemaError(ValueError):
    """A message does not conform to the stream-json schema."""


@dataclass(frozen=True)
class StreamJsonEventSchema:
    event_type: str
    subtype: str
    required_fields: tuple[str, ...]
    envelope: tuple[str, ...] = ()

    def as_json_schema(self) -> dict[str, object]:
        required = tuple(dict.fromkeys(
            ("type", "subtype", *self.required_fields, *self.envelope)))
        properties: dict[str, object] = {
            "type": {"const": self.event_type},
            "subtype": {"const": self.subtype},
        }
        for field in required:
            field_type = STREAM_JSON_FIELD_TYPES.get(field)
            if field_type is None:
                continue
            properties[field] = field_type
        return {
            "type": "object",
            "required": list(required),
            "properties": properties,
            "additionalProperties": True,
        }


_FIELD_STRING: dict[str, object] = {"type": ["string", "null"]}
_FIELD_INTEGER: dict[str, object] = {"type": "integer"}
_FIELD_NUMBER: dict[str, object] = {"type": "number"}
_FIELD_BOOLEAN: dict[str, object] = {"type": "boolean"}
_FIELD_OBJECT: dict[str, object] = {"type": ["object", "null"]}
_FIELD_ARRAY: dict[str, object] = {"type": "array"}
_FIELD_STRING_OR_ARRAY: dict[str, object] = {"type": ["string", "array"]}

STREAM_JSON_FIELD_TYPES: Mapping[str, Mapping[str, object]] = {
    "type": {"type": "string"},
    "subtype": {"type": "string"},
    "protocol": {"type": "string"},
    "protocol_version": {"type": "string"},
    "session_id": _FIELD_STRING,
    "turn_id": {"type": "string"},
    "request_id": {"type": "string"},
    "sequence": _FIELD_INTEGER,
    "job_sequence": _FIELD_INTEGER,
    "step": _FIELD_INTEGER,
    "pid": {"type": ["integer", "null"]},
    "exit_code": {"type": ["integer", "null"]},
    "generation": _FIELD_INTEGER,
    "before_tokens": _FIELD_INTEGER,
    "after_tokens": _FIELD_INTEGER,
    "tokens": _FIELD_INTEGER,
    "active_index": _FIELD_INTEGER,
    "conflict_count": _FIELD_INTEGER,
    "high_watermark_tokens": _FIELD_INTEGER,
    "low_watermark_tokens": _FIELD_INTEGER,
    "compacted_count": _FIELD_INTEGER,
    "preserved_count": _FIELD_INTEGER,
    "protected_count": _FIELD_INTEGER,
    "summarized_count": _FIELD_INTEGER,
    "referenced_count": _FIELD_INTEGER,
    "agent_sequence": _FIELD_INTEGER,
    "confidence": _FIELD_NUMBER,
    "content": _FIELD_STRING_OR_ARRAY,
    "text": {"type": "string"},
    "error": {"type": "string"},
    "context": {"type": "string"},
    "result": {"type": "string"},
    "reason": {"type": "string"},
    "prompt": {"type": "string"},
    "message": {"type": ["object", "string"]},
    "tool_call": _FIELD_OBJECT,
    "snapshot": {"type": "object"},
    "data": _FIELD_OBJECT,
    "usage": _FIELD_OBJECT,
    "ledger": _FIELD_OBJECT,
    "scope_counts": {"type": "object"},
    "source_savings": {"type": "object"},
    "event": {"type": "string"},
    "cwd": {"type": "string"},
    "provider": {"type": "string"},
    "model": {"type": "string"},
    "mode": {"type": "string"},
    "permission_mode": {"type": "string"},
    "tools": _FIELD_ARRAY,
    "mcp_servers": _FIELD_ARRAY,
    "options": _FIELD_ARRAY,
    "items": _FIELD_ARRAY,
    "statuses": _FIELD_ARRAY,
    "entry_ids": _FIELD_ARRAY,
    "is_error": _FIELD_BOOLEAN,
    "matched": _FIELD_BOOLEAN,
    "blocked": _FIELD_BOOLEAN,
    "allow_free_text": _FIELD_BOOLEAN,
    "answered": _FIELD_BOOLEAN,
    "phase": {"type": "string"},
    "detail": {"type": "string"},
    "tool_call_id": {"type": "string"},
    "tool_name": {"type": "string"},
    "stream": {"type": "string"},
    "permission_id": {"type": "string"},
    "candidate_id": {"type": "string"},
    "status": {"type": "string"},
    "trigger": {"type": "string"},
    "digest": {"type": "string"},
    "memory_id": {"type": "string"},
    "action": {"type": "string"},
    "scope": {"type": "string"},
    "kind": {"type": "string"},
    "checkpoint_id": {"type": "string"},
    "summary_digest": {"type": "string"},
    "strategy": {"type": "string"},
    "summary_source": {"type": "string"},
    "source": {"type": "string"},
    "tool": {"type": "string"},
    "system_message": {"type": "string"},
    "agent_id": {"type": "string"},
    "task": {"type": "string"},
    "definition": {"type": "string"},
    "preview": {"type": "string"},
    "input_id": {"type": "string"},
    "explanation": {"type": "string"},
    "command": {"type": "string"},
    "job_id": {"type": "string"},
    "application": _FIELD_OBJECT,
    "security": _FIELD_OBJECT,
}

_ENVELOPE = ("turn_id", "sequence", "session_id")
_JOB_ENVELOPE = ("session_id",)


def _event(
    event_type: str,
    subtype: str,
    *required: str,
    envelope: Sequence[str] = _ENVELOPE,
) -> StreamJsonEventSchema:
    return StreamJsonEventSchema(
        event_type=event_type,
        subtype=subtype,
        required_fields=tuple(required),
        envelope=tuple(envelope),
    )


OUTPUT_EVENT_SCHEMAS: Mapping[tuple[str, str], StreamJsonEventSchema] = {
    ("system", "init"): _event(
        "system", "init", "protocol", "protocol_version", "session_id", "cwd",
        "provider", "model", "mode", "permission_mode", "tools", "mcp_servers",
        envelope=(),
    ),
    ("system", "session_end"): _event(
        "system", "session_end", "session_id", "reason", envelope=()),
    ("system", "turn_started"): _event("system", "turn_started", "content"),
    ("step", "started"): _event("step", "started", "step"),
    ("query", "phase_changed"): _event(
        "query", "phase_changed", "phase", "detail"),
    ("assistant", "text_delta"): _event(
        "assistant", "text_delta", "text"),
    ("assistant", "thinking_delta"): _event(
        "assistant", "thinking_delta", "text"),
    ("assistant", "message"): _event(
        "assistant", "message", "message", "usage"),
    ("tool", "started"): _event("tool", "started", "tool_call"),
    ("tool", "progress"): _event(
        "tool", "progress", "tool_call_id", "tool_name", "message", "data"),
    ("tool", "output_delta"): _event(
        "tool", "output_delta", "tool_call_id", "tool_name", "text", "stream"),
    ("tool", "completed"): _event(
        "tool", "completed", "tool_call", "content", "is_error", "data"),
    ("permission", "requested"): _event(
        "permission", "requested", "tool_call", "reason", "permission_id"),
    ("permission", "denied"): _event(
        "permission", "denied", "tool_call", "reason"),
    ("permission", "security_decision"): _event(
        "permission", "security_decision", "tool_call_id", "security"),
    ("context", "assembled"): _event(
        "context", "assembled", "event", "snapshot"),
    ("context", "dropped"): _event(
        "context", "dropped", "event", "items", "reason"),
    ("context", "microcompacted"): _event(
        "context", "microcompacted", "trigger", "before_tokens", "after_tokens",
        "compacted_count", "preserved_count", "protected_count",
        "summarized_count", "referenced_count"),
    ("context", "compressed"): _event(
        "context", "compressed", "before_tokens", "after_tokens", "strategy",
        "summary_source", "generation", "high_watermark_tokens",
        "low_watermark_tokens", "source_savings", "checkpoint_id"),
    ("memory", "loaded"): _event(
        "memory", "loaded", "event", "entry_ids", "scope_counts", "tokens",
        "source"),
    ("memory", "candidate"): _event(
        "memory", "candidate", "event", "candidate_id", "status", "trigger",
        "confidence", "conflict_count", "digest"),
    ("memory", "written"): _event(
        "memory", "written", "event", "memory_id", "action", "scope", "kind",
        "tokens", "source"),
    ("compact", "checkpoint"): _event(
        "compact", "checkpoint", "event", "checkpoint_id", "generation",
        "before_tokens", "after_tokens", "summary_digest", "strategy", "trigger",
        "source_savings"),
    ("hook", "triggered"): _event(
        "hook", "triggered", "trigger", "matched", "blocked", "tool", "reason",
        "system_message"),
    ("job", "started"): _event(
        "job", "started", "job_id", "command", "pid", "cwd",
        envelope=_JOB_ENVELOPE),
    ("job", "output_delta"): _event(
        "job", "output_delta", "job_id", "job_sequence", "text",
        envelope=_JOB_ENVELOPE),
    ("job", "finished"): _event(
        "job", "finished", "job_id", "command", "status", "exit_code", "preview",
        envelope=_JOB_ENVELOPE),
    ("subagent", "started"): _event(
        "subagent", "started", "agent_id", "task", "definition"),
    ("subagent", "feed"): _event(
        "subagent", "feed", "agent_id", "agent_sequence", "kind", "text",
        "data"),
    ("subagent", "state"): _event(
        "subagent", "state", "agent_id", "status", "reason"),
    ("subagent", "finished"): _event(
        "subagent", "finished", "agent_id", "status", "preview"),
    ("user", "steered"): _event("user", "steered", "content"),
    ("plan", "updated"): _event(
        "plan", "updated", "items", "active_index", "statuses", "explanation"),
    ("input", "requested"): _event(
        "input", "requested", "input_id", "prompt", "options",
        "allow_free_text"),
    ("input", "resolved"): _event(
        "input", "resolved", "input_id", "answered"),
    ("result", "success"): _event(
        "result", "success", "is_error", "result", "turn_id", "session_id",
        envelope=(),
    ),
    ("result", "cancelled"): _event(
        "result", "cancelled", "is_error", "result", "turn_id", "session_id",
        envelope=(),
    ),
    ("result", "error"): _event(
        "result", "error", "is_error", "result", "turn_id", "session_id",
        envelope=(),
    ),
    ("error", "occurred"): _event("error", "occurred", "error", "context"),
}

INPUT_EVENT_SCHEMAS: Mapping[tuple[str, str], StreamJsonEventSchema] = {
    ("user", "message"): StreamJsonEventSchema(
        "user", "message", (), envelope=()),
    ("control", "end"): StreamJsonEventSchema(
        "control", "end", (), envelope=()),
    ("control", "interrupt"): StreamJsonEventSchema(
        "control", "interrupt", (), envelope=()),
    ("control", "permission_response"): StreamJsonEventSchema(
        "control", "permission_response", ("request_id", "decision"), envelope=()),
    ("control", "user_input_response"): StreamJsonEventSchema(
        "control", "user_input_response", ("request_id", "action"), envelope=()),
}


def _json_type_matches(value: object, schema: Mapping[str, object]) -> bool:
    expected = schema.get("type")
    if expected is None:
        return True
    if isinstance(expected, str):
        allowed = (expected,)
    elif isinstance(expected, (list, tuple)):
        allowed = tuple(expected)
    else:
        return False
    for item in allowed:
        if item == "null" and value is None:
            return True
        if item == "string" and isinstance(value, str):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "object" and isinstance(value, Mapping):
            return True
        if item == "array" and isinstance(value, (list, tuple)):
            return True
    return False


def _validate_required_fields(
    message: Mapping[str, object],
    schema: StreamJsonEventSchema,
    *,
    include_subtype: bool,
) -> None:
    prefix = ("type", "subtype") if include_subtype else ("type",)
    required = tuple(dict.fromkeys(
        (*prefix, *schema.required_fields, *schema.envelope)))
    missing = [field for field in required if field not in message]
    if missing:
        label = f"{schema.event_type}/{schema.subtype}"
        raise StreamJsonSchemaError(
            f"{label} 缺少字段: {', '.join(missing)}")


def _validate_known_field_types(
    message: Mapping[str, object],
    schema: StreamJsonEventSchema,
) -> None:
    for field, field_schema in STREAM_JSON_FIELD_TYPES.items():
        if field in message and not _json_type_matches(message[field], field_schema):
            raise StreamJsonSchemaError(
                f"{schema.event_type}/{schema.subtype}.{field} 类型不符合 schema")


def validate_stream_event(message: Mapping[str, object]) -> None:
    """Validate one encoder-produced output event against the v1 schema."""
    event_type = message.get("type")
    subtype = message.get("subtype")
    if not isinstance(event_type, str) or not event_type:
        raise StreamJsonSchemaError("stream event.type 必须是非空字符串")
    if not isinstance(subtype, str) or not subtype:
        raise StreamJsonSchemaError("stream event.subtype 必须是非空字符串")

    schema: StreamJsonEventSchema
    if event_type == "event":
        schema = StreamJsonEventSchema(
            "event", subtype, (), envelope=_ENVELOPE)
    else:
        matched_schema = OUTPUT_EVENT_SCHEMAS.get((event_type, subtype))
        if matched_schema is None:
            raise StreamJsonSchemaError(
                f"未知 stream event: {event_type}/{subtype}")
        schema = matched_schema
    _validate_required_fields(message, schema, include_subtype=True)
    _validate_known_field_types(message, schema)
    if event_type == "system" and subtype == "init":
        if message.get("protocol") != STREAM_JSON_PROTOCOL_NAME:
            raise StreamJsonSchemaError("system/init.protocol 不匹配")
        if message.get("protocol_version") != STREAM_JSON_PROTOCOL_VERSION:
            raise StreamJsonSchemaError("system/init.protocol_version 不匹配")


def validate_input_message(message: Mapping[str, object]) -> None:
    """Validate the recognized envelope of one stream-json stdin message."""
    message_type = message.get("type")
    if message_type == "user":
        subtype = "message"
    elif message_type == "control":
        raw_subtype = message.get("subtype")
        subtype = str(raw_subtype).strip() if raw_subtype is not None else ""
        if subtype not in {
            item for event_type, item in INPUT_EVENT_SCHEMAS
            if event_type == "control"
        }:
            raise StreamJsonSchemaError(f"未知 control.subtype: {subtype!r}")
    else:
        raise StreamJsonSchemaError("type 必须为 user 或 control")
    schema = INPUT_EVENT_SCHEMAS.get((str(message_type), subtype))
    if schema is None:
        raise StreamJsonSchemaError(f"未知 input event: {message_type}/{subtype}")

    if message_type == "user":
        has_content = "content" in message
        has_message = "message" in message
        if not has_content and not has_message:
            raise StreamJsonSchemaError("user 消息必须包含 content 或 message")
        if has_content and not isinstance(message["content"], (str, list, tuple)):
            raise StreamJsonSchemaError(
                "user.content 必须是字符串或 content block 数组")
        if has_message:
            envelope = message["message"]
            if not isinstance(envelope, Mapping):
                raise StreamJsonSchemaError("message 必须是对象")
            if envelope.get("role", "user") != "user":
                raise StreamJsonSchemaError("message.role 必须为 user")
        if "request_id" in message and not isinstance(message["request_id"], str):
            raise StreamJsonSchemaError("user.request_id 必须是字符串")
        return

    _validate_required_fields(message, schema, include_subtype=True)
    _validate_known_field_types(message, schema)


def _input_event_json_schema(schema: StreamJsonEventSchema) -> dict[str, object]:
    if schema.event_type == "user":
        return {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"const": "user"},
                "request_id": {"type": "string"},
                "content": _FIELD_STRING_OR_ARRAY,
                "message": {"type": "object"},
            },
            "anyOf": [
                {"required": ["content"]},
                {"required": ["message"]},
            ],
            "additionalProperties": True,
        }
    required = tuple(dict.fromkeys(
        ("type", "subtype", *schema.required_fields)))
    properties: dict[str, object] = {
        "type": {"const": "control"},
        "subtype": {"const": schema.subtype},
    }
    for field in required:
        field_type = STREAM_JSON_FIELD_TYPES.get(field)
        if field_type is not None:
            properties[field] = field_type
    return {
        "type": "object",
        "required": list(required),
        "properties": properties,
        "additionalProperties": True,
    }


def stream_json_schema_document() -> dict[str, object]:
    """Return a stable, serializable JSON-Schema-style protocol document."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://krelcode.dev/schemas/stream-json-v1.json",
        "title": "Krelcode Stream JSON v1",
        "x-krelcode-schema-version": STREAM_JSON_SCHEMA_VERSION,
        "x-krelcode-protocol": STREAM_JSON_PROTOCOL_NAME,
        "x-krelcode-protocol-version": STREAM_JSON_PROTOCOL_VERSION,
        "oneOf": [
            schema.as_json_schema()
            for _key, schema in sorted(OUTPUT_EVENT_SCHEMAS.items())
        ] + [
            {
                "type": "object",
                "properties": {
                    "type": {"const": "event"},
                    "subtype": {"type": "string", "minLength": 1},
                },
                "required": ["type", "subtype", *_ENVELOPE],
                "additionalProperties": True,
            },
        ],
        "$defs": {
            "input": [
                _input_event_json_schema(schema)
                for _key, schema in sorted(INPUT_EVENT_SCHEMAS.items())
            ],
        },
    }


__all__ = [
    "INPUT_EVENT_SCHEMAS",
    "OUTPUT_EVENT_SCHEMAS",
    "STREAM_JSON_FIELD_TYPES",
    "STREAM_JSON_PROTOCOL_NAME",
    "STREAM_JSON_PROTOCOL_VERSION",
    "STREAM_JSON_SCHEMA_VERSION",
    "StreamJsonEventSchema",
    "StreamJsonSchemaError",
    "stream_json_schema_document",
    "validate_input_message",
    "validate_stream_event",
]
