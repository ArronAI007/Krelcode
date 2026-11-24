from __future__ import annotations

from krelcode.agent.query import QueryEvent
from krelcode.core.events import (
    LLMStreamDelta,
    RunStarted,
    ToolCallCompleted,
)
from krelcode.core.messages import ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.session.envelope import SessionEnvelope
from krelcode.session.event_router import SessionEventRouter
from krelcode.session.state import SessionViewState


def test_session_envelope_is_content_light_and_redacted():
    redactor = SecretRedactor()
    redactor.add("sk-router-secret")
    event = ToolCallCompleted(
        ToolCall("call-sk-router-secret", "read_file", {}),
        content="sk-router-secret and file contents",
        is_error=False,
    )

    envelope = SessionEnvelope.from_query_event(
        QueryEvent("turn-7", 3, event),
        session_id="session-sk-router-secret",
        redactor=redactor,
    )

    assert envelope.turn_id == "turn-7"
    assert envelope.sequence == 3
    assert envelope.event_type == "ToolCallCompleted"
    assert envelope.payload["content_chars"] == len(event.content)
    assert envelope.payload["tool_name"] == "read_file"
    assert "content" not in envelope.payload
    assert "sk-router-secret" not in repr(envelope)


def test_event_router_applies_state_then_dispatches_both_channels():
    order = []
    state = SessionViewState()
    envelopes = []
    queries = []
    router = SessionEventRouter(
        state,
        session_id="s1",
        envelope_sinks=(lambda env: (order.append("envelope"), envelopes.append(env)),),
        query_sinks=(lambda query: (order.append("query"), queries.append(query)),),
    )
    query_event = QueryEvent("turn-1", 0, RunStarted("hello"))

    envelope = router.route(query_event)

    assert state.is_running
    assert state.current_turn_id == "turn-1"
    assert order == ["envelope", "query"]
    assert envelopes == [envelope]
    assert queries == [query_event]
    assert router.last_envelope is envelope


def test_llm_delta_payload_records_size_not_text():
    envelope = SessionEnvelope.from_query_event(
        QueryEvent("turn-1", 1, LLMStreamDelta("private model output")),
        session_id="s1",
    )

    assert envelope.event_type == "LLMStreamDelta"
    assert envelope.payload == {"text_chars": len("private model output")}


def test_repl_session_runner_routes_envelopes_through_shared_router():
    import asyncio

    from krelcode.session.repl_session import ReplSessionRunner

    class FakeQuery:
        async def _events(self):
            yield QueryEvent("turn-1", 0, RunStarted("hello"))
            yield QueryEvent("turn-1", 1, LLMStreamDelta("answer"))

        @property
        def events(self):
            return self._events()

    seen = []
    state = SessionViewState()
    runner = ReplSessionRunner(
        controller=None,
        state=state,
        on_event=lambda _event: None,
        envelope_sink=seen.append,
        session_id="session-1",
    )

    asyncio.run(runner._consume(FakeQuery()))

    assert [item.event_type for item in seen] == [
        "RunStarted", "LLMStreamDelta",
    ]
    assert runner.last_envelope is seen[-1]
    assert runner.event_router.last_envelope is seen[-1]
