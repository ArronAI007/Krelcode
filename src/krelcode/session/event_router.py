"""Route QueryEvents through the reducer and content/observation sinks."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Optional

from ..agent.query import QueryEvent
from ..core.redact import SecretRedactor
from ..core.session_events import SessionEvent
from .envelope import SessionEnvelope
from .state import SessionEventReducer, SessionViewState

QueryEventSink = Callable[[QueryEvent], None]
SessionEventSink = Callable[[SessionEvent], None]
EnvelopeSink = Callable[[SessionEnvelope], None]


class SessionEventRouter:
    """Single dispatch point for state, foreground UI, and observers.

    The raw typed event remains available to renderers.  Envelope sinks receive
    a content-light representation suitable for recording and telemetry.
    """

    def __init__(
        self,
        state: SessionViewState,
        *,
        session_id: str = "",
        reducer: Optional[SessionEventReducer] = None,
        event_sinks: Iterable[SessionEventSink] = (),
        query_sinks: Iterable[QueryEventSink] = (),
        envelope_sinks: Iterable[EnvelopeSink] = (),
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.state = state
        self.session_id = session_id
        self.reducer = reducer or SessionEventReducer()
        self.event_sinks = tuple(event_sinks)
        self.query_sinks = tuple(query_sinks)
        self.envelope_sinks = tuple(envelope_sinks)
        self.redactor = redactor
        self.last_event: Optional[SessionEvent] = None
        self.last_envelope: Optional[SessionEnvelope] = None

    def route(self, query_event: QueryEvent) -> SessionEnvelope:
        self.reducer.apply(query_event, self.state)
        envelope = SessionEnvelope.from_query_event(
            query_event,
            session_id=self.session_id,
            redactor=self.redactor,
        )
        self.last_event = query_event
        self.last_envelope = envelope
        for event_sink in self.event_sinks:
            event_sink(query_event)
        for envelope_sink in self.envelope_sinks:
            envelope_sink(envelope)
        for query_sink in self.query_sinks:
            query_sink(query_event)
        return envelope

    def subscribe_event(self, sink: SessionEventSink) -> None:
        """Subscribe to the canonical typed session event."""

        self.event_sinks = (*self.event_sinks, sink)

    def subscribe_query(self, sink: QueryEventSink) -> None:
        self.query_sinks = (*self.query_sinks, sink)

    def subscribe_envelope(self, sink: EnvelopeSink) -> None:
        self.envelope_sinks = (*self.envelope_sinks, sink)


__all__ = [
    "EnvelopeSink", "QueryEventSink", "SessionEventRouter", "SessionEventSink",
]
