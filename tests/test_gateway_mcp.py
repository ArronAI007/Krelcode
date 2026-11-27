"""MCP gateway tool ACL and quota isolation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from krelcode.contracts.gateway import (
    GatewayContext,
    GatewayLayer,
    MCPGatewayRequest,
    TenantQuota,
)
from krelcode.gateway.mcp import MCPGateway
from krelcode.gateway.quota import InMemoryTenantQuota


class FakeClient:
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> Mapping[str, object]:
        return {"name": name, "arguments": arguments, "isError": False}


class FakeSupervisor:
    def __init__(self) -> None:
        self.clients = {"github": FakeClient()}
        self.authorized: list[str] = []

    async def authorize(self, name: str) -> None:
        self.authorized.append(name)

    def status(self) -> Sequence[Mapping[str, object]]:
        return ({"name": "github", "alive": True},)


def test_mcp_gateway_executes_allowed_tool_and_audits() -> None:
    gateway = MCPGateway(
        FakeSupervisor(),
        allowed_tools={"github": {"search"}},
    )
    request = MCPGatewayRequest(
        context=GatewayContext(request_id="req-1", tenant_id="tenant"),
        server="github",
        tool="search",
        arguments={"q": "krelcode"},
    )

    result = asyncio.run(gateway.execute(request))

    assert result.allowed
    assert result.succeeded
    assert result.audit_id.startswith("mcp-")
    assert isinstance(result.result, Mapping)


def test_mcp_gateway_denies_unlisted_tool() -> None:
    gateway = MCPGateway(
        FakeSupervisor(),
        allowed_tools={"github": {"read_file"}},
    )
    request = MCPGatewayRequest(
        context=GatewayContext(request_id="req-1"),
        server="github",
        tool="delete_repo",
    )

    result = asyncio.run(gateway.execute(request))

    assert not result.allowed
    assert not result.succeeded
    assert "ACL" in result.reason


def test_mcp_request_quota_does_not_consume_llm_tokens() -> None:
    quota = InMemoryTenantQuota(TenantQuota(
        max_concurrent=1,
        max_requests_per_minute=10,
        max_tokens_per_minute=10,
    ))
    assert quota.reserve(
        "tenant", tokens=10, layer=GatewayLayer.LLM).allowed
    gateway = MCPGateway(FakeSupervisor(), quota=quota)

    result = asyncio.run(gateway.execute(MCPGatewayRequest(
        context=GatewayContext(request_id="req-1", tenant_id="tenant"),
        server="github",
        tool="search",
    )))

    assert result.allowed
    assert quota.snapshot("tenant", layer=GatewayLayer.MCP)[
        "tokens_in_window"] == 0
