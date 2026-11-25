"""Typed AgentEvent -> stable, redacted web-facing EventEnvelope."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import fields
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.core.events import (
    AgentEvent,
    ApprovalRequested,
    ContextAssembled,
    ContextCheckpointCreated,
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
from krelcode.core.messages import (
    ImageBlock,
    Message,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolCall,
)
from krelcode.core.redact import SecretRedactor

from .models import EventEnvelope, stable_hash

_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")

_KIND_BY_EVENT: tuple[tuple[type[AgentEvent], str], ...] = (
    (RunStarted, "run"),
    (StepStarted, "step"),
    (LLMStreamDelta, "model"),
    (LLMThinkingDelta, "model"),
    (LLMResponseReceived, "model"),
    (QueryPhaseChanged, "query"),
    (ToolCallStarted, "tool"),
    (ToolProgress, "tool"),
    (ToolOutputDelta, "tool"),
    (ToolCallCompleted, "tool"),
    (HookTriggered, "hook"),
    (ApprovalRequested, "permission"),
    (PermissionDenied, "permission"),
    (ContextMicrocompacted, "context"),
    (ContextCompressed, "context"),
    (ContextCheckpointCreated, "context"),
    (ContextAssembled, "context"),
    (ContextDropped, "context"),
    (MemoryLoaded, "memory"),
    (MemoryCandidateObserved, "memory"),
    (MemoryWritten, "memory"),
    (PlanUpdated, "plan"),
    (UserInputRequested, "interaction"),
    (UserInputResolved, "interaction"),
    (ShellJobStarted, "job"),
    (ShellJobOutput, "job"),
    (ShellJobFinished, "job"),
    (SubagentSpawned, "subagent"),
    (SubagentFeedUpdated, "subagent"),
    (SubagentStateChanged, "subagent"),
    (SubagentFinished, "subagent"),
    (UserSteered, "interaction"),
    (RunFinished, "run"),
    (RunCancelled, "run"),
    (ErrorOccurred, "error"),
)


def _snake_case(value: str) -> str:
    first = _SNAKE_1.sub(r"\1_\2", value)
    return _SNAKE_2.sub(r"\1_\2", first).lower()


def event_kind(event: AgentEvent) -> str:
    for event_type, kind in _KIND_BY_EVENT:
        if isinstance(event, event_type):
            return kind
    return "event"


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _safe_identifier(value: object) -> str:
    text = str(_enum_value(value) or "").strip()
    return text if len(text) <= 200 else text[:200]


class EventProjector:
    """Project one run's typed events without retaining event bodies."""

    def __init__(
        self,
        run_id: str,
        redactor: SecretRedactor,
        *,
        max_string_chars: int = 4_000,
        max_collection_items: int = 100,
        include_text_previews: bool = True,
    ) -> None:
        self.run_id = run_id
        self.redactor = redactor
        self.max_string_chars = max(128, min(int(max_string_chars), 100_000))
        self.max_collection_items = max(1, min(int(max_collection_items), 1_000))
        self.include_text_previews = bool(include_text_previews)
        self.session_id = ""
        self.turn_id = ""
        self.step = 0

    def project(self, event: AgentEvent, sequence: int) -> EventEnvelope:
        self._track_identity(event)
        payload = self._payload(event)
        span_id, parent_span_id = self._span(event)
        return EventEnvelope(
            event_id=uuid.uuid4().hex,
            run_id=self.run_id,
            sequence=sequence,
            timestamp=time.time(),
            session_id=self.session_id,
            turn_id=self.turn_id,
            step=self.step,
            span_id=span_id,
            parent_span_id=parent_span_id,
            kind=event_kind(event),
            name=_snake_case(type(event).__name__),
            payload=payload,
        )

    def _track_identity(self, event: AgentEvent) -> None:
        event_session = getattr(event, "session_id", "")
        if isinstance(event_session, str) and event_session:
            self.session_id = event_session
        event_turn = getattr(event, "turn_id", "")
        if isinstance(event_turn, str) and event_turn:
            self.turn_id = event_turn
        if isinstance(event, StepStarted):
            self.step = max(0, int(event.step))

    def _span(self, event: AgentEvent) -> tuple[str, str]:
        if isinstance(event, RunStarted):
            return self.run_id, ""
        if isinstance(event, (ToolCallStarted, ToolCallCompleted)):
            tool_id = _safe_identifier(event.tool_call.id)
            if tool_id:
                return tool_id, self.run_id
        if isinstance(event, StepStarted):
            return f"step-{event.step}", self.run_id
        if self.step:
            return f"step-{self.step}", self.run_id
        return self.run_id, ""

    def _payload(self, event: AgentEvent) -> dict[str, Any]:
        if isinstance(event, RunStarted):
            return {
                "input": self._text(event.user_input),
                "input_chars": len(event.user_input),
            }
        if isinstance(event, (LLMStreamDelta, LLMThinkingDelta)):
            return {"text": self._text(event.text), "text_chars": len(event.text)}
        if isinstance(event, LLMResponseReceived):
            return {
                "message": self._message(event.message),
                "usage": self._value(event.usage),
            }
        if isinstance(event, ToolCallStarted):
            return {"tool_call": self._tool_call(event.tool_call)}
        if isinstance(event, ToolProgress):
            return {
                "tool_call_id": self._text(event.tool_call_id),
                "tool_name": self._text(event.tool_name),
                "message": self._text(event.message),
                "data": self._value(event.data),
            }
        if isinstance(event, ToolOutputDelta):
            return {
                "tool_call_id": self._text(event.tool_call_id),
                "tool_name": self._text(event.tool_name),
                "stream": self._text(event.stream),
                "text": self._text(event.text),
                "text_chars": len(event.text),
            }
        if isinstance(event, ToolCallCompleted):
            return {
                "tool_call": self._tool_call(event.tool_call),
                "content": self._text(event.content),
                "content_chars": len(event.content),
                "is_error": bool(event.is_error),
                "data": self._value(event.data),
                "blocks": self._blocks(event.blocks),
            }
        if isinstance(event, (ApprovalRequested, PermissionDenied)):
            return {
                "tool_call": self._tool_call(event.tool_call),
                "reason": self._text(event.reason),
                **({"request_id": self._text(event.request_id)}
                   if isinstance(event, ApprovalRequested) else {}),
            }
        if isinstance(event, ContextAssembled):
            return {
                "snapshot": self._context_snapshot(event.snapshot),
                "ledger": self._value(event.ledger or {}),
            }
        return self._generic_fields(event)

    def _generic_fields(self, event: AgentEvent) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for definition in fields(event):
            value = getattr(event, definition.name)
            if definition.name in {"snapshot", "message", "tool_call"}:
                continue
            payload[definition.name] = self._value(value)
        return payload

    def _context_snapshot(self, snapshot: ContextUsageSnapshot) -> dict[str, Any]:
        value = self._value(snapshot.as_record())
        return value if isinstance(value, dict) else {}

    def _tool_call(self, tool_call: ToolCall) -> dict[str, Any]:
        return {
            "id": self._text(tool_call.id),
            "name": self._text(tool_call.name),
            "arguments": self._value(tool_call.arguments),
        }

    def _message(self, message: Message) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self._text(message.role),
            "content": self._text(message.content or ""),
            "content_chars": len(message.content or ""),
        }
        if message.tool_calls:
            result["tool_calls"] = [
                self._tool_call(call)
                for call in message.tool_calls[:self.max_collection_items]
            ]
        if message.tool_call_id:
            result["tool_call_id"] = self._text(message.tool_call_id)
        if message.tool_name:
            result["tool_name"] = self._text(message.tool_name)
        if message.tool_is_error is not None:
            result["tool_is_error"] = bool(message.tool_is_error)
        if message.images:
            result["image_count"] = len(message.images)
        if message.tool_blocks:
            result["blocks"] = self._blocks(message.tool_blocks)
        return result

    def _blocks(self, blocks: Sequence[object]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for block in list(blocks)[:self.max_collection_items]:
            if isinstance(block, TextBlock):
                output.append({"type": "text", "text": self._text(block.text)})
            elif isinstance(block, ImageBlock):
                output.append({
                    "type": "image",
                    "media_type": self._text(block.media_type),
                    "alt": self._text(block.alt),
                    "bytes": len(block.data_b64),
                })
            elif isinstance(block, ResourceBlock):
                output.append({
                    "type": "resource",
                    "uri": self._text(block.uri),
                    "name": self._text(block.name or ""),
                    "mime_type": self._text(block.mime_type or ""),
                    "text_chars": len(block.text or ""),
                })
            elif isinstance(block, StructuredBlock):
                output.append({
                    "type": "structured",
                    "data": self._value(block.data),
                })
            else:
                output.append({"type": "unknown"})
        return output

    def _text(self, value: object) -> str:
        raw = self.redactor.redact(str(_enum_value(value) or ""))
        if not self.include_text_previews:
            return ""
        if len(raw) <= self.max_string_chars:
            return raw
        return raw[: self.max_string_chars - 1] + "…"

    def _value(self, value: object, *, depth: int = 0) -> Any:
        if depth > 6:
            return "<depth-limit>"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self._text(value)
        if isinstance(value, Path):
            return self._text(value)
        if isinstance(value, Enum):
            return self._value(value.value, depth=depth + 1)
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= self.max_collection_items:
                    output["__truncated__"] = True
                    break
                safe_key = self._text(key)
                output[safe_key] = self._value(item, depth=depth + 1)
            return output
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            sequence_output = [
                self._value(item, depth=depth + 1)
                for item in items[:self.max_collection_items]
            ]
            if len(items) > self.max_collection_items:
                sequence_output.append("<truncated>")
            return sequence_output
        return self._text(value)

    def payload_hash(self, payload: Mapping[str, Any]) -> str:
        return stable_hash(dict(payload))


__all__ = ["EventProjector", "event_kind"]
