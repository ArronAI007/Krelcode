"""LLM gateway routing, fallback and quota boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator, Sequence

import pytest

from krelcode.contracts.gateway import (
    GatewayContext,
    GatewayLayer,
    TenantQuota,
)
from krelcode.core.messages import Message
from krelcode.gateway.llm import LLMGateway, LLMGatewayDenied
from krelcode.gateway.quota import InMemoryTenantQuota
from krelcode.llm.base import (
    EvidenceChannel,
    LLMEvent,
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
    ToolSpec,
)


class FakeProvider:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.capabilities = ProviderCapabilities()
        self.fail = fail
        self.calls = 0

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        *,
        evidence: EvidenceChannel | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        yield TextDelta("ok")
        yield StreamFinished(
            Message.assistant("ok"),
            usage={"input_tokens": 2, "output_tokens": 1},
        )


def test_llm_gateway_falls_back_before_first_event() -> None:
    primary = FakeProvider("primary", fail=True)
    fallback = FakeProvider("fallback")
    gateway = LLMGateway(
        {"primary": primary, "fallback": fallback},
        default_provider="primary",
        fallback_providers=("fallback",),
    )

    async def run() -> list[LLMEvent]:
        return [
            event async for event in gateway.stream(
                [Message.user("hello")])
        ]

    events = asyncio.run(run())

    assert primary.calls == 1
    assert fallback.calls == 1
    assert any(
        isinstance(event, StreamFinished) for event in events)


def test_llm_gateway_denies_when_token_quota_is_exhausted() -> None:
    provider = FakeProvider("primary")
    quota = InMemoryTenantQuota(TenantQuota(
        max_concurrent=2,
        max_requests_per_minute=10,
        max_tokens_per_minute=1,
    ))
    gateway = LLMGateway(
        {"primary": provider},
        quota=quota,
    )

    async def run() -> None:
        async for _event in gateway.stream([Message.user("hello")]):
            pass

    with pytest.raises(LLMGatewayDenied):
        asyncio.run(run())
    assert provider.calls == 0


def test_llm_and_mcp_token_quota_windows_are_isolated() -> None:
    quota = InMemoryTenantQuota(TenantQuota(
        max_concurrent=1,
        max_requests_per_minute=10,
        max_tokens_per_minute=10,
    ))
    llm = quota.reserve(
        "tenant", tokens=10, layer=GatewayLayer.LLM)
    mcp = quota.reserve(
        "tenant", tokens=0, layer=GatewayLayer.MCP)

    assert llm.allowed
    assert mcp.allowed
    assert quota.snapshot("tenant", layer=GatewayLayer.LLM)[
        "tokens_in_window"] == 10
    assert quota.snapshot("tenant", layer=GatewayLayer.MCP)[
        "tokens_in_window"] == 0
