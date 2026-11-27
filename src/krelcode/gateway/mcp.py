"""MCP gateway: server/tool ACL, credentials delegation and tool-call audit."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence, Set
from typing import Optional, Protocol, cast

from krelcode.contracts.gateway import (
    GatewayAuditPort,
    GatewayDecision,
    GatewayLayer,
    MCPGatewayPort,
    MCPGatewayRequest,
    MCPGatewayResult,
    TenantQuotaPort,
)
from .audit import NullGatewayAudit
from .quota import normalize_quota_decision


class MCPGatewayError(RuntimeError):
    """Base error for MCP gateway execution."""


class _MCPClientPort(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> Mapping[str, object]: ...


class _MCPSupervisorPort(Protocol):
    @property
    def clients(self) -> Mapping[str, _MCPClientPort]: ...

    async def authorize(self, name: str) -> None: ...

    def status(self) -> Sequence[Mapping[str, object]]: ...


ToolPolicy = Callable[[MCPGatewayRequest], bool]


class MCPGateway(MCPGatewayPort):
    """Central policy facade over the existing MCP protocol implementation.

    The gateway never owns MCP transport, OAuth token storage or tool schema
    discovery.  It authorizes one ``tools/call`` and delegates credentials to
    the supervisor/client that already owns them.
    """

    def __init__(
        self,
        supervisor: _MCPSupervisorPort | object,
        *,
        allowed_tools: Optional[
            Mapping[str, object] | Sequence[str]
        ] = None,
        tool_policy: Optional[ToolPolicy] = None,
        quota: Optional[TenantQuotaPort] = None,
        audit: Optional[GatewayAuditPort] = None,
    ) -> None:
        self.supervisor = cast(_MCPSupervisorPort, supervisor)
        self.allowed_tools = allowed_tools
        self.tool_policy = tool_policy
        self.quota = quota
        self.audit = audit or NullGatewayAudit()

    @property
    def server_registry(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self.supervisor.status())

    async def authorize(self, server: str) -> None:
        await self.supervisor.authorize(server)

    def set_allowed_tools(
        self,
        allowed_tools: Optional[Mapping[str, object] | Sequence[str]],
    ) -> None:
        self.allowed_tools = allowed_tools

    async def execute(self, request: MCPGatewayRequest) -> MCPGatewayResult:
        audit_id = f"mcp-{uuid.uuid4().hex}"
        decision = self._authorize(request, audit_id)
        if not decision.allowed:
            self._audit(
                request,
                "mcp.deny",
                decision,
                {"audit_id": audit_id},
            )
            return MCPGatewayResult(
                allowed=False,
                succeeded=False,
                reason=decision.reason,
                audit_id=audit_id,
                decision=decision,
            )
        reserved = self._reserve(request)
        if not reserved.allowed:
            self._audit(
                request,
                "mcp.admit",
                reserved,
                {"audit_id": audit_id},
            )
            return MCPGatewayResult(
                allowed=False,
                succeeded=False,
                reason=reserved.reason,
                audit_id=audit_id,
                decision=reserved,
            )
        client = self.supervisor.clients.get(request.server)
        if client is None:
            reason = f"未知或未连接的 MCP server: {request.server}"
            failed = GatewayDecision(
                layer=GatewayLayer.MCP,
                allowed=False,
                reason=reason,
                route=f"mcp:{request.server}/{request.tool}",
            )
            self._audit(
                request,
                "mcp.unavailable",
                failed,
                {"audit_id": audit_id},
            )
            if self.quota is not None:
                _release_quota(self.quota, request.context.tenant_id)
            return MCPGatewayResult(
                allowed=False,
                succeeded=False,
                reason=reason,
                audit_id=audit_id,
                decision=failed,
            )
        started_at = time.monotonic()
        try:
            result = await client.call_tool(
                request.tool,
                dict(request.arguments),
            )
            self._audit(
                request,
                "mcp.complete",
                decision,
                {
                    "audit_id": audit_id,
                    "server": request.server,
                    "tool": request.tool,
                    "duration_ms": int(
                        (time.monotonic() - started_at) * 1000),
                },
            )
            return MCPGatewayResult(
                allowed=True,
                succeeded=True,
                result=result,
                audit_id=audit_id,
                decision=decision,
            )
        except Exception as exc:
            self._audit(
                request,
                "mcp.error",
                decision,
                {
                    "audit_id": audit_id,
                    "server": request.server,
                    "tool": request.tool,
                    "error_type": type(exc).__name__,
                },
            )
            return MCPGatewayResult(
                allowed=True,
                succeeded=False,
                reason=f"{type(exc).__name__}: {exc}",
                audit_id=audit_id,
                decision=decision,
                metadata={
                    "server": request.server,
                    "tool": request.tool,
                },
            )
        finally:
            if self.quota is not None:
                _release_quota(self.quota, request.context.tenant_id)

    def _authorize(
        self,
        request: MCPGatewayRequest,
        audit_id: str,
    ) -> GatewayDecision:
        reason = self._policy_reason(request)
        if reason:
            return GatewayDecision(
                layer=GatewayLayer.MCP,
                allowed=False,
                reason=reason,
                route=f"mcp:{request.server}/{request.tool}",
                metadata={"audit_id": audit_id},
            )
        return GatewayDecision(
            layer=GatewayLayer.MCP,
            allowed=True,
            route=f"mcp:{request.server}/{request.tool}",
            metadata={
                "audit_id": audit_id,
                "server": request.server,
                "tool": request.tool,
            },
        )

    def _policy_reason(self, request: MCPGatewayRequest) -> str:
        allowed_servers = _string_values(
            request.context.metadata.get("mcp_servers"))
        if allowed_servers and request.server not in allowed_servers:
            return f"MCP server 不在租户允许列表: {request.server}"
        context_tools = _string_values(
            request.context.metadata.get("mcp_tool_allowlist"))
        if context_tools and not (
            request.tool in context_tools
            or f"{request.server}:{request.tool}" in context_tools
            or f"{request.server}__{request.tool}" in context_tools
        ):
            return f"MCP tool 不在上下文允许列表: {request.tool}"
        if self.tool_policy is not None and not self.tool_policy(request):
            return f"MCP tool 被 gateway policy 拒绝: {request.tool}"
        allowed = self.allowed_tools
        if allowed is None:
            return ""
        if isinstance(allowed, Mapping):
            if "*" in allowed:
                return "" if _tool_entry_allows(
                    allowed.get("*"), request.tool) else (
                        f"MCP tool 不在全局允许列表: {request.tool}")
            if request.server not in allowed:
                return f"MCP server 不在工具 ACL: {request.server}"
            return "" if _tool_entry_allows(
                allowed.get(request.server), request.tool) else (
                    f"MCP tool 不在工具 ACL: {request.server}/{request.tool}")
        names = _string_values(allowed)
        if request.tool not in names and (
            f"{request.server}:{request.tool}" not in names
        ):
            return f"MCP tool 不在工具 ACL: {request.tool}"
        return ""

    def _reserve(self, request: MCPGatewayRequest) -> GatewayDecision:
        if self.quota is None:
            return GatewayDecision(
                layer=GatewayLayer.MCP,
                allowed=True,
                route="quota:disabled",
            )
        reserve = getattr(self.quota, "reserve", None)
        if not callable(reserve):
            return GatewayDecision(
                layer=GatewayLayer.MCP,
                allowed=True,
                route="quota:unavailable",
            )
        try:
            decision = reserve(
                request.context.tenant_id,
                tokens=0,
                cost_usd=0.0,
                layer=GatewayLayer.MCP,
            )
        except TypeError:
            decision = reserve(
                request.context.tenant_id,
                tokens=0,
                cost_usd=0.0,
            )
        return normalize_quota_decision(decision, GatewayLayer.MCP)

    def _audit(
        self,
        request: MCPGatewayRequest,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None:
        self.audit.record(
            GatewayLayer.MCP,
            request.context,
            action,
            decision,
            metadata,
        )


def _string_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if (
        isinstance(value, (Sequence, Set))
        and not isinstance(value, (bytes, bytearray, str))
    ):
        return tuple(str(item) for item in value if str(item))
    return ()


def _tool_entry_allows(value: object, tool: str) -> bool:
    if value is None or value == "*":
        return True
    if isinstance(value, str):
        return value == tool
    return tool in _string_values(value)


def _release_quota(
    quota: TenantQuotaPort,
    tenant_id: str,
) -> None:
    release = getattr(quota, "release", None)
    if not callable(release):
        return
    try:
        release(tenant_id, layer=GatewayLayer.MCP)
    except TypeError:
        release(tenant_id)


__all__ = ["MCPGateway", "MCPGatewayError", "ToolPolicy"]
