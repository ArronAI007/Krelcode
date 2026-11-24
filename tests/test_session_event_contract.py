from __future__ import annotations

import inspect

import pytest

from krelcode.agent.query import QueryEvent
from krelcode.core import events as event_module
from krelcode.core.events import (
    AgentEvent,
    LLMStreamDelta,
    RunStarted,
    ToolCallCompleted,
)
from krelcode.core.messages import ToolCall
from krelcode.core.session_events import (
    SESSION_EVENT_TYPES,
    SESSION_EVENT_TYPE_TO_KIND,
    SessionEvent,
    SessionEventKind,
    as_session_event_payload,
)
from krelcode.session.envelope import SessionEnvelope
from krelcode.session.events import SessionEvent as PublicSessionEvent
from krelcode.session.event_router import SessionEventRouter
from krelcode.session.state import SessionViewState


def test_query_event_is_compatibility_alias_for_session_event():
    assert QueryEvent is SessionEvent
    assert PublicSessionEvent is SessionEvent

    event = SessionEvent(
        "turn-1",
        2,
        RunStarted("hello"),
        session_id="session-1",
    )

    assert event.event_type == "RunStarted"
    assert event.kind is SessionEventKind.RUN
    assert not event.is_terminal
    assert event.with_session_id("session-2").session_id == "session-2"


def test_every_concrete_agent_event_has_a_stable_session_family():
    concrete = {
        value
        for _name, value in inspect.getmembers(event_module, inspect.isclass)
        if value is not AgentEvent
        and issubclass(value, AgentEvent)
        and value.__module__ == event_module.__name__
    }

    assert set(SESSION_EVENT_TYPES) == concrete
    assert set(SESSION_EVENT_TYPE_TO_KIND) == concrete


def test_query_engine_mapper_rejects_unclassified_event_payload():
    with pytest.raises(TypeError, match="unsupported session event payload"):
        as_session_event_payload(AgentEvent())


def test_session_envelope_is_projection_of_canonical_event():
    event = SessionEvent(
        "turn-4",
        7,
        LLMStreamDelta("private output"),
        session_id="session-4",
        timestamp=123.5,
    )

    envelope = SessionEnvelope.from_session_event(event)

    assert envelope.session_id == "session-4"
    assert envelope.turn_id == "turn-4"
    assert envelope.sequence == 7
    assert envelope.event_type == "LLMStreamDelta"
    assert envelope.event_kind is SessionEventKind.MODEL
    assert envelope.payload == {"text_chars": len("private output")}
    assert envelope.timestamp == 123.5
    assert "private output" not in repr(envelope)


def test_router_exposes_canonical_event_before_content_light_projection():
    order: list[str] = []
    state = SessionViewState()
    router = SessionEventRouter(
        state,
        session_id="session-9",
        event_sinks=(lambda event: order.append("event"),),
        envelope_sinks=(lambda envelope: order.append("envelope"),),
    )
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    event = SessionEvent(
        "turn-9",
        3,
        ToolCallCompleted(call, "contents", False),
        session_id="session-9",
    )

    envelope = router.route(event)

    assert order == ["event", "envelope"]
    assert router.last_event is event
    assert router.last_envelope is envelope
    assert envelope.event_kind is SessionEventKind.TOOL
    assert state.active_tool_id == ""
    assert state.tool_calls_completed == 1
