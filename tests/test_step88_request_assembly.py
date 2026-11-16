"""Step 88: budgeted, cache-aware assembly of the exact provider request."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from fakes import FakeTransport, sse
from krelcode.agent.compression import ContextManager
from krelcode.agent.context_usage import build_context_usage_snapshot
from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.agent.tokens import estimate_history_tokens, estimate_text_tokens
from krelcode.core.context_usage import CacheSegmentSnapshot
from krelcode.core.events import ContextAssembled, ContextCompressed
from krelcode.core.messages import Message
from krelcode.llm.base import (
    PreparedLLMRequest,
    StreamFinished,
    TextDelta,
    collect,
)
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.gemini import GeminiProvider
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.providers.openai import OpenAIProvider
from krelcode.llm.retry import RetryPolicy, RetryingProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry

TOOLS = [{
    "name": "read_file",
    "description": "Read one file",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}]


def _empty_sse_openai():
    return sse({"choices": [{"delta": {"content": "ok"}}]})


def test_anthropic_prepared_snapshot_tracks_all_three_cache_segments_without_content():
    provider = AnthropicProvider(
        model="claude-x", api_key="sk-ant-secret", prompt_cache=True,
        transport=FakeTransport(b""))
    messages = [
        Message.system(
            "STABLE SENTINEL\n\nDYNAMIC SENTINEL",
            provider_meta={"prompt_cache_boundaries": [len("STABLE SENTINEL")]},
        ),
        Message.user("inspect"),
        Message.assistant("ready"),
    ]

    prepared = provider.prepare_request(messages, TOOLS)
    snapshot = build_context_usage_snapshot(
        messages,
        turn_id="turn-v88",
        step=1,
        tool_specs=TOOLS,
        prepared_request=prepared,
        budget_tokens=10_000,
        model_context_window=200_000,
    )
    body = prepared.body.decode("utf-8")

    assert snapshot.wire_fidelity == "exact"
    assert snapshot.provider == "anthropic"
    assert snapshot.cache_mode == "explicit"
    assert snapshot.request_body_bytes == len(prepared.body)
    assert snapshot.request_body_tokens == estimate_text_tokens(body)
    assert snapshot.estimated_input_tokens == snapshot.request_body_tokens
    assert snapshot.output_reserve_tokens == provider.max_tokens
    assert snapshot.output_limit_tokens == provider.max_tokens
    assert [segment.kind for segment in snapshot.cache_segments] == [
        "system", "tools", "messages"]
    assert snapshot.cache_segment_count == 3
    assert snapshot.cache_control_segment_count == 3
    assert snapshot.tool_cacheable_tokens > 0
    assert snapshot.message_cacheable_tokens > 0
    stable = "STABLE SENTINEL"
    assert snapshot.stable_hash == hashlib.sha256(stable.encode()).hexdigest()
    assert snapshot.cache_boundary_chars == len(stable)
    assert snapshot.cache_segments[0].digest == snapshot.stable_hash

    record = json.dumps(snapshot.as_record(), ensure_ascii=False)
    assert "STABLE SENTINEL" not in record
    assert "DYNAMIC SENTINEL" not in record
    assert "sk-ant-secret" not in record
    assert "read_file" not in record


def test_openai_and_gemini_snapshots_match_exact_wire_body():
    cases = [
        (
            OpenAIProvider(
                model="m1", api_key="sk-openai",
                transport=FakeTransport(_empty_sse_openai())),
            "implicit",
        ),
        (
            GeminiProvider(
                model="gemini-2.5-flash", api_key="gem",
                transport=FakeTransport(sse(
                    {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))),
            "none",
        ),
    ]
    messages = [
        Message.system(
            "STABLE\n\nDYNAMIC",
            provider_meta={"prompt_cache_boundaries": [len("STABLE")]},
        ),
        Message.user("question"),
    ]

    for provider, cache_mode in cases:
        prepared = provider.prepare_request(messages, TOOLS)
        snapshot = build_context_usage_snapshot(
            messages,
            turn_id="turn-wire",
            step=1,
            tool_specs=TOOLS,
            prepared_request=prepared,
        )
        assert snapshot.wire_fidelity == "exact"
        assert snapshot.cache_mode == cache_mode
        assert snapshot.request_body_bytes == len(prepared.body)
        assert snapshot.request_body_tokens == estimate_text_tokens(
            prepared.body.decode("utf-8"))
        assert snapshot.estimated_input_tokens == snapshot.request_body_tokens
        assert sum(snapshot.source_token_map.values()) == (
            snapshot.estimated_input_tokens)

        async def run_prepared():
            return [event async for event in provider.stream_prepared(prepared)]

        events = asyncio.run(run_prepared())
        assert any(isinstance(event, StreamFinished) for event in events)
        assert provider._transport.last.body == prepared.body


def test_prepared_request_repr_cannot_leak_body_headers_or_prompt():
    prepared = PreparedLLMRequest(
        provider="test",
        model="m",
        url="https://example.invalid/secret",
        headers={"Authorization": "Bearer TOP-SECRET"},
        body=b'{"prompt":"PRIVATE PROMPT"}',
        request_body_tokens=10,
        payload_digest="a" * 64,
    )

    rendered = repr(prepared)
    assert "TOP-SECRET" not in rendered
    assert "PRIVATE PROMPT" not in rendered
    assert "example.invalid" not in rendered
    assert "body_bytes=27" in rendered


class FlakyPreparedProvider:
    name = "flaky-prepared"
    model = "m"
    capabilities = MockProvider().capabilities

    def __init__(self):
        self.prepared = 0
        self.executed = []
        self.attempts = 0

    def prepare_request(self, messages, tools=None):
        self.prepared += 1
        return PreparedLLMRequest(
            provider=self.name,
            model=self.model,
            url="https://example.invalid/v1",
            headers={"Authorization": "Bearer secret"},
            body=json.dumps({"count": self.prepared}).encode(),
            request_body_tokens=5,
            payload_digest="digest",
        )

    async def stream_prepared(self, prepared):
        self.executed.append(prepared)
        self.attempts += 1
        if self.attempts == 1:
            from krelcode.core.errors import LLMHTTPError
            raise LLMHTTPError(503, "temporary", self.name)
        yield TextDelta("ok")
        yield StreamFinished(Message.assistant("ok"), "stop", {})

    async def stream(self, messages, tools=None):
        prepared = self.prepare_request(messages, tools)
        async for event in self.stream_prepared(prepared):
            yield event


def test_retrying_provider_retries_the_same_prepared_request():
    inner = FlakyPreparedProvider()
    provider = RetryingProvider(
        inner, RetryPolicy(max_retries=1, base_delay=0),
        sleep=lambda _delay: asyncio.sleep(0))

    finished = asyncio.run(collect(provider, [Message.user("hello")]))

    assert finished.message.content == "ok"
    assert inner.prepared == 1
    assert inner.attempts == 2
    assert inner.executed[0] is inner.executed[1]
    assert provider.supports_prepared_requests


class ProbePreparedProvider:
    """Adds a fixed wire/context overhead and records assemble-once behaviour."""

    name = "probe"
    model = "probe-model"
    capabilities = MockProvider().capabilities

    def __init__(self, overhead=500):
        self.overhead = overhead
        self.prepared_requests = []
        self.executed = []
        self.replies = ["compressed summary", "done"]

    def prepare_request(self, messages, tools=None):
        tokens = estimate_history_tokens(messages) + self.overhead
        prepared = PreparedLLMRequest(
            provider=self.name,
            model=self.model,
            url="https://example.invalid/v1",
            headers={},
            body=b"{}",
            request_body_tokens=tokens,
            payload_digest=f"digest-{len(self.prepared_requests)}",
            output_limit_tokens=100,
            cache_mode="none",
        )
        self.prepared_requests.append(prepared)
        return prepared

    async def stream_prepared(self, prepared):
        self.executed.append(prepared)
        text = self.replies.pop(0) if self.replies else "done"
        yield TextDelta(text)
        yield StreamFinished(Message.assistant(text), "stop", {})

    async def stream(self, messages, tools=None):
        prepared = self.prepare_request(messages, tools)
        async for event in self.stream_prepared(prepared):
            yield event


def test_real_request_overhead_triggers_one_compact_then_reassembles(tmp_path):
    provider = ProbePreparedProvider(overhead=500)
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
    )
    for index in range(6):
        loop.history.extend([
            Message.user(f"question-{index}"),
            Message.assistant(f"answer-{index}"),
        ])
    history_only = estimate_history_tokens(loop.history)
    loop.context_manager = ContextManager(
        provider, max_tokens=history_only + 470, keep_recent=2)
    events = []
    loop.bus.subscribe(events.append)

    answer = asyncio.run(loop.run("final question", turn_id="turn-compact"))

    assert answer == "done"
    compressed = [e for e in events if isinstance(e, ContextCompressed)]
    assert len(compressed) == 1
    assert not loop.context_manager.over_budget(
        loop.history, extra_tokens=500)
    # initial probe + summary request + final request; no compaction loop.
    assert len(provider.prepared_requests) == 3
    assert len(provider.executed) == 2
    assert provider.executed[-1] is provider.prepared_requests[-1]
    assert loop.last_context_snapshot is not None
    assert loop.last_context_snapshot.wire_fidelity == "exact"
    assert loop.last_context_snapshot.input_fits


def test_full_request_budget_accounts_for_output_reserve_even_when_history_fits():
    manager = ContextManager(MockProvider(), max_tokens=10_000)
    history = [Message.user("small")]

    assert not manager.over_budget(history)
    assert manager.over_budget(
        history,
        extra_tokens=850,
        output_reserve_tokens=200,
        model_context_window=1_000,
    )
    assert manager.preview_compact(
        history,
        force=False,
        extra_tokens=850,
        output_reserve_tokens=200,
        model_context_window=1_000,
    ).eligible is False  # too short to summarize, but the need was detected


def test_legacy_provider_falls_back_to_estimated_snapshot(tmp_path):
    loop = AgentLoop(
        llm=MockProvider(fallback="legacy ok"),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
    )

    assert asyncio.run(loop.run("hello", turn_id="turn-legacy")) == "legacy ok"

    snapshot = loop.last_context_snapshot
    assert snapshot is not None
    assert snapshot.wire_fidelity == "estimated"
    assert snapshot.cache_mode == "estimated"
    assert snapshot.request_body_bytes == 0
    assert snapshot.provider == "mock"
    assert snapshot.input_fits



def test_stream_json_exposes_safe_wire_and_cache_accounting():
    snapshot = build_context_usage_snapshot(
        [Message.user("hello")],
        turn_id="turn-json",
        step=2,
        prepared_request=PreparedLLMRequest(
            provider="anthropic",
            model="claude-x",
            url="https://example.invalid",
            headers={"x-api-key": "TOP-SECRET"},
            body=b'{"secret":"PRIVATE PROMPT"}',
            request_body_tokens=12,
            payload_digest="abc123",
            output_limit_tokens=4096,
            cache_mode="explicit",
            cache_segments=(CacheSegmentSnapshot(
                "system", 8, 32, "stable-digest", True),),
            stable_cache_chars=32,
            stable_cache_hash="stable-digest",
        ),
        budget_tokens=1_000,
        model_context_window=200_000,
    )
    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="session-88",
        cwd="/repo",
        provider="anthropic",
        model="claude-x",
        run_mode="stream-json",
    ))

    encoded = encoder.encode(QueryEvent(
        turn_id="turn-json",
        sequence=1,
        event=ContextAssembled(snapshot),
    ))
    payload = encoded["snapshot"]

    assert payload["wire_fidelity"] == "exact"
    assert payload["request_body_bytes"] == len(
        b'{"secret":"PRIVATE PROMPT"}')
    assert payload["cache_segments"][0]["digest"] == "stable-digest"
    assert payload["input_fits"] is True
    assert "PRIVATE PROMPT" not in json.dumps(encoded, ensure_ascii=False)
    assert "TOP-SECRET" not in json.dumps(encoded, ensure_ascii=False)
