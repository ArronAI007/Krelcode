"""Policy-enforcement contracts for LLM, MCP and Agent gateways.

The three gateway layers deliberately have different governance subjects:

* LLM gateway: one model request (route, cost, token rate and fallback).
* MCP gateway: one MCP tool call (server/tool ACL, credentials and audit).
* Agent gateway: one task (tenant concurrency, agent route and task budget).

Sharing identity and correlation fields does not mean sharing quotas.  A
tool call must never consume a model-token budget, and a task tenant quota must
not silently override a tool permission decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Protocol

from ..core.messages import ImageRef
from .application import ApplicationRequest, TaskBudget


class GatewayLayer(str, Enum):
    LLM = "llm"
    MCP = "mcp"
    AGENT = "agent"


@dataclass(frozen=True)
class GatewayContext:
    """Identity, tenant and budget shared by every gateway layer."""

    request_id: str
    tenant_id: str = "local"
    principal_id: str = "local"
    budget: TaskBudget = field(default_factory=TaskBudget)
    metadata: Mapping[str, object] = field(default_factory=dict)
    session_id: str = ""
    turn_id: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "tenant_id",
            "principal_id",
            "session_id",
            "turn_id",
            "trace_id",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "budget": self.budget.to_dict(),
        }


@dataclass(frozen=True)
class GatewayDecision:
    layer: GatewayLayer
    allowed: bool
    reason: str = ""
    route: str = ""
    quota_remaining: Optional[int] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "layer", GatewayLayer(self.layer))
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(self, "route", str(self.route or "").strip())
        if self.quota_remaining is not None:
            object.__setattr__(
                self, "quota_remaining", max(0, int(self.quota_remaining)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class LLMGatewayRequest:
    context: GatewayContext
    model: str = ""
    provider: str = ""
    estimated_input_tokens: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)
    estimated_output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", str(self.model or "").strip())
        object.__setattr__(self, "provider", str(self.provider or "").strip())
        object.__setattr__(
            self,
            "estimated_input_tokens",
            max(0, int(self.estimated_input_tokens)),
        )
        object.__setattr__(
            self,
            "estimated_output_tokens",
            max(0, int(self.estimated_output_tokens)),
        )
        object.__setattr__(
            self,
            "estimated_cost_usd",
            max(0.0, float(self.estimated_cost_usd)),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class LLMGatewayRoute:
    provider_name: str
    model: str
    fallback_providers: tuple[str, ...] = ()
    decision: Optional[GatewayDecision] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_name", str(self.provider_name or "").strip())
        object.__setattr__(self, "model", str(self.model or "").strip())
        object.__setattr__(
            self,
            "fallback_providers",
            tuple(
                dict.fromkeys(
                    str(name).strip()
                    for name in self.fallback_providers
                    if str(name).strip()
                )
            ),
        )


@dataclass(frozen=True)
class MCPGatewayRequest:
    context: GatewayContext
    server: str
    tool: str
    arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "server", str(self.server or "").strip())
        object.__setattr__(self, "tool", str(self.tool or "").strip())
        if not self.server:
            raise ValueError("MCPGatewayRequest.server 不能为空")
        if not self.tool:
            raise ValueError("MCPGatewayRequest.tool 不能为空")
        object.__setattr__(
            self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True)
class MCPGatewayResult:
    allowed: bool
    result: object | None = None
    reason: str = ""
    audit_id: str = ""
    decision: Optional[GatewayDecision] = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    succeeded: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "succeeded", bool(self.succeeded))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(self, "audit_id", str(self.audit_id or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class AgentGatewayRequest:
    context: GatewayContext
    message: str
    session_id: str = ""
    agent_definition: str = ""
    route_key: str = ""
    application: Optional[ApplicationRequest] = None
    images: tuple[ImageRef, ...] = ()
    tool_allowlist: Optional[tuple[str, ...]] = None
    reminder_ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", str(self.message or "").strip())
        if not self.message:
            raise ValueError("AgentGatewayRequest.message 不能为空")
        object.__setattr__(
            self, "session_id", str(self.session_id or "").strip())
        object.__setattr__(
            self,
            "agent_definition",
            str(self.agent_definition or "").strip(),
        )
        object.__setattr__(
            self, "route_key", str(self.route_key or "").strip())
        object.__setattr__(self, "images", tuple(self.images or ()))
        if self.tool_allowlist is not None:
            object.__setattr__(
                self,
                "tool_allowlist",
                tuple(dict.fromkeys(
                    str(name).strip()
                    for name in self.tool_allowlist
                    if str(name).strip()
                )),
            )
        object.__setattr__(
            self,
            "reminder_ids",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.reminder_ids
                if str(item).strip()
            )),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class AgentGatewayResult:
    accepted: bool
    task_id: str = ""
    route: str = ""
    reason: str = ""
    handle: object | None = None
    decision: Optional[GatewayDecision] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted", bool(self.accepted))
        object.__setattr__(self, "task_id", str(self.task_id or "").strip())
        object.__setattr__(self, "route", str(self.route or "").strip())
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


class LLMGatewayPort(Protocol):
    def route(self, request: LLMGatewayRequest) -> LLMGatewayRoute: ...


class MCPGatewayPort(Protocol):
    async def execute(self, request: MCPGatewayRequest) -> MCPGatewayResult: ...


class AgentGatewayPort(Protocol):
    async def submit(self, request: AgentGatewayRequest) -> AgentGatewayResult: ...


@dataclass(frozen=True)
class TenantQuota:
    max_concurrent: int = 2
    max_requests_per_minute: int = 60
    max_tokens_per_minute: int = 200_000
    max_cost_usd_per_task: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_concurrent", max(1, int(self.max_concurrent)))
        object.__setattr__(
            self,
            "max_requests_per_minute",
            max(1, int(self.max_requests_per_minute)),
        )
        object.__setattr__(
            self,
            "max_tokens_per_minute",
            max(1, int(self.max_tokens_per_minute)),
        )
        if self.max_cost_usd_per_task is not None:
            object.__setattr__(
                self,
                "max_cost_usd_per_task",
                max(0.0, float(self.max_cost_usd_per_task)),
            )


class TenantQuotaPort(Protocol):
    def reserve(
        self,
        tenant_id: str,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> GatewayDecision: ...

    def release(self, tenant_id: str) -> None: ...


class GatewayAuditPort(Protocol):
    def record(
        self,
        layer: GatewayLayer,
        context: GatewayContext,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None: ...


class GatewayPorts(Protocol):
    """Cohesive access surface for the three independent gateway planes."""

    @property
    def llm(self) -> LLMGatewayPort: ...

    @property
    def mcp(self) -> MCPGatewayPort: ...

    @property
    def agent(self) -> AgentGatewayPort: ...


__all__ = [
    "AgentGatewayPort",
    "AgentGatewayRequest",
    "AgentGatewayResult",
    "GatewayAuditPort",
    "GatewayContext",
    "GatewayDecision",
    "GatewayLayer",
    "GatewayPorts",
    "LLMGatewayPort",
    "LLMGatewayRequest",
    "LLMGatewayRoute",
    "MCPGatewayPort",
    "MCPGatewayRequest",
    "MCPGatewayResult",
    "TenantQuota",
    "TenantQuotaPort",
]
