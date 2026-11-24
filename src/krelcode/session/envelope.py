"""Stable, content-light envelopes for session-level event routing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..agent.query import QueryEvent
from ..core.events import AgentEvent
from ..core.redact import SecretRedactor
from ..core.session_events import SessionEvent, SessionEventKind, session_event_kind


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value[:200]
    return None


def event_payload(
    event: AgentEvent,
    *,
    redactor: Optional[SecretRedactor] = None,
) -> dict[str, Any]:
    """Return routing metadata without copying prompt/tool output bodies.

    Consumers that need rich rendering continue to receive the typed
    ``AgentEvent`` through a query-event sink.  Envelope observers are for
    state, metrics, persistence, and diagnostics and therefore receive only
    small identifiers/counters.
    """
    payload: dict[str, Any] = {}
    common_fields = (
        "step", "phase", "turn_id", "request_id", "tool_call_id",
        "tool_name", "is_error", "job_id", "agent_id", "checkpoint_id",
        "memory_id", "candidate_id", "generation", "tokens", "status",
    )
    for name in common_fields:
        value = _safe_scalar(getattr(event, name, None))
        if value not in (None, ""):
            payload[name] = value

    for name in ("text", "content", "user_input", "command", "preview"):
        value = getattr(event, name, None)
        if isinstance(value, str):
            payload[f"{name}_chars"] = len(value)

    tool_call = getattr(event, "tool_call", None)
    if tool_call is not None:
        tool_id = _safe_scalar(getattr(tool_call, "id", None))
        tool_name = _safe_scalar(getattr(tool_call, "name", None))
        if tool_id:
            payload["tool_call_id"] = tool_id
        if tool_name:
            payload["tool_name"] = tool_name

    if hasattr(event, "items"):
        items = getattr(event, "items", ())
        try:
            payload["item_count"] = len(items)
        except TypeError:
            pass
    if hasattr(event, "options"):
        options = getattr(event, "options", ())
        try:
            payload["option_count"] = len(options)
        except TypeError:
            pass
    usage = getattr(event, "usage", None)
    if isinstance(usage, Mapping):
        payload["usage"] = {
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

    if redactor is not None:
        for key, value in tuple(payload.items()):
            if isinstance(value, str):
                payload[key] = redactor.redact(value)
    return payload


@dataclass(frozen=True)
class SessionEnvelope:
    """One routed query event with stable session identity and metadata."""

    session_id: str
    turn_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_kind: SessionEventKind = SessionEventKind.RUN
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_session_event(
        cls,
        session_event: SessionEvent,
        *,
        redactor: Optional[SecretRedactor] = None,
    ) -> "SessionEnvelope":
        event = session_event.event
        session_id = session_event.session_id
        return cls(
            session_id=redactor.redact(session_id) if redactor else session_id,
            turn_id=session_event.turn_id,
            sequence=session_event.sequence,
            event_type=session_event.event_type,
            payload=event_payload(event, redactor=redactor),
            event_kind=session_event_kind(event),
            timestamp=session_event.timestamp,
        )

    @classmethod
    def from_query_event(
        cls,
        query_event: QueryEvent,
        *,
        session_id: str = "",
        redactor: Optional[SecretRedactor] = None,
    ) -> "SessionEnvelope":
        """Compatibility entry point for the historical query-event name."""

        bound = query_event.with_session_id(session_id)
        return cls.from_session_event(bound, redactor=redactor)


__all__ = ["SessionEnvelope", "event_payload"]
