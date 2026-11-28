"""Agent gateway: tenant task admission, routing and session continuity."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from collections.abc import Set
from typing import Mapping, Optional, Sequence

from krelcode.contracts.application import (
    TaskBudget,
)
from krelcode.contracts.agent import AgentContractFactory
from krelcode.contracts.gateway import (
    AgentGatewayPort,
    AgentGatewayRequest,
    AgentGatewayResult,
    GatewayAuditPort,
    GatewayDecision,
    GatewayLayer,
    TenantQuotaPort,
)
from krelcode.contracts.runtime import (
    RuntimeIdentity,
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskRuntimePort,
    RuntimeTaskState,
    RuntimeSandboxBinding,
)
from .audit import NullGatewayAudit
from .quota import normalize_quota_decision


class AgentGatewayError(RuntimeError):
    """Base error for agent-task gateway execution."""


@dataclass(frozen=True)
class AgentRoute:
    name: str
    executor: RuntimeTaskExecutorPort
    agent_definition: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name or "").strip())
        if not self.name:
            raise ValueError("AgentRoute.name 不能为空")
        object.__setattr__(
            self,
            "agent_definition",
            str(self.agent_definition or self.name).strip(),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


class _QuotaReleasingExecutor(RuntimeTaskExecutorPort):
    def __init__(
        self,
        executor: RuntimeTaskExecutorPort,
        quota: Optional[TenantQuotaPort],
        tenant_id: str,
    ) -> None:
        self.executor = executor
        self.quota = quota
        self.tenant_id = tenant_id
        self._released = False

    async def execute(self, task: RuntimeTask) -> str:
        try:
            return await self.executor.execute(task)
        finally:
            self.release()

    def release(self) -> None:
        if self.quota is None or self._released:
            return
        self._released = True
        release = getattr(self.quota, "release", None)
        if not callable(release):
            return
        try:
            release(self.tenant_id, layer=GatewayLayer.AGENT)
        except TypeError:
            release(self.tenant_id)


class AgentGateway(AgentGatewayPort):
    """Task-scoped gateway over a production ``RuntimeTaskRuntimePort``.

    It does not assemble prompts, execute tools or inspect Sandbox internals.
    It admits a task, selects an agent route and hands a ``RuntimeTask`` to the
    runtime, which owns queueing, leases, timeout and recovery.
    """

    def __init__(
        self,
        runtime: RuntimeTaskRuntimePort,
        *,
        default_executor: Optional[RuntimeTaskExecutorPort] = None,
        routes: Optional[
            Mapping[str, RuntimeTaskExecutorPort | AgentRoute]
        ] = None,
        allowed_agents: Optional[Sequence[str]] = None,
        quota: Optional[TenantQuotaPort] = None,
        audit: Optional[GatewayAuditPort] = None,
    ) -> None:
        self.runtime = runtime
        self.default_executor = default_executor
        self.routes = {
            str(key): (
                value if isinstance(value, AgentRoute)
                else AgentRoute(name=str(key), executor=value)
            )
            for key, value in (routes or {}).items()
        }
        self.allowed_agents = (
            tuple(dict.fromkeys(
                str(item).strip()
                for item in allowed_agents or ()
                if str(item).strip()
            ))
            if allowed_agents is not None else None
        )
        self.quota = quota
        self.audit = audit or NullGatewayAudit()

    async def submit(
        self,
        request: AgentGatewayRequest,
    ) -> AgentGatewayResult:
        route = self._resolve_route(request)
        if route is None:
            decision = GatewayDecision(
                layer=GatewayLayer.AGENT,
                allowed=False,
                reason="没有可用的 agent route",
                route=request.route_key or request.agent_definition,
            )
            self._audit(request, "agent.route", decision, {})
            return AgentGatewayResult(
                accepted=False,
                route=decision.route,
                reason=decision.reason,
                decision=decision,
            )
        policy_reason = self._policy_reason(request, route)
        if policy_reason:
            decision = GatewayDecision(
                layer=GatewayLayer.AGENT,
                allowed=False,
                reason=policy_reason,
                route=route.name,
            )
            self._audit(request, "agent.deny", decision, {})
            return AgentGatewayResult(
                accepted=False,
                route=route.name,
                reason=policy_reason,
                decision=decision,
            )
        application = request.application
        contract = request.agent_contract
        if (
            contract is not None
            and application is not None
            and contract.intent != application
        ):
            decision = GatewayDecision(
                layer=GatewayLayer.AGENT,
                allowed=False,
                reason="application 与 agent_contract.intent 不一致",
                route=route.name,
            )
            self._audit(request, "agent.deny", decision, {})
            return AgentGatewayResult(
                accepted=False,
                route=route.name,
                reason=decision.reason,
                decision=decision,
            )
        if contract is not None:
            application = contract.intent
            if (
                contract.principal.principal_id
                != request.context.principal_id
                or contract.principal.tenant_id != request.context.tenant_id
            ):
                decision = GatewayDecision(
                    layer=GatewayLayer.AGENT,
                    allowed=False,
                    reason="AgentContract 身份与 GatewayContext 不一致",
                    route=route.name,
                )
                self._audit(request, "agent.deny", decision, {})
                return AgentGatewayResult(
                    accepted=False,
                    route=route.name,
                    reason=decision.reason,
                    decision=decision,
                )
        if application is None:
            decision = GatewayDecision(
                layer=GatewayLayer.AGENT,
                allowed=False,
                reason="AgentGateway 必须提交显式 ApplicationRequest",
                route=route.name,
            )
            self._audit(request, "agent.deny", decision, {
                "reason": "missing_application_request",
            })
            return AgentGatewayResult(
                accepted=False,
                route=route.name,
                reason=decision.reason,
                decision=decision,
            )
        contract = contract or AgentContractFactory.from_application(
            application,
            principal=RuntimeIdentity(
                principal_id=request.context.principal_id,
                tenant_id=request.context.tenant_id,
            ),
            sandbox=RuntimeSandboxBinding(),
            tool_allowlist=request.tool_allowlist,
            metadata={
                "gateway_request_id": request.context.request_id,
                **dict(request.metadata),
            },
        )
        input_errors = contract.validate_inputs(
            request.message, request.images)
        if input_errors:
            decision = GatewayDecision(
                layer=GatewayLayer.AGENT,
                allowed=False,
                reason="AgentContract 输入无效: " + "; ".join(input_errors),
                route=route.name,
            )
            self._audit(request, "agent.deny", decision, {})
            return AgentGatewayResult(
                accepted=False,
                route=route.name,
                reason=decision.reason,
                decision=decision,
            )
        route_decision = GatewayDecision(
            layer=GatewayLayer.AGENT,
            allowed=True,
            route=route.name,
            metadata={
                "agent_definition": route.agent_definition,
                "session_id": request.session_id,
            },
        )
        reserved = self._reserve(request, application.budget)
        if not reserved.allowed:
            self._audit(request, "agent.admit", reserved, {
                "route": route.name,
            })
            return AgentGatewayResult(
                accepted=False,
                route=route.name,
                reason=reserved.reason,
                decision=reserved,
            )
        turn_id = f"turn-{uuid.uuid4().hex}"
        resolved_tool_allowlist = (
            request.tool_allowlist
            if request.tool_allowlist is not None
            else contract.authority.allowed_tools
        )
        deadline_at = _deadline(
            application.budget.timeout_seconds,
            request.context.budget.timeout_seconds,
        )
        task = RuntimeTask(
            turn_id=turn_id,
            message=request.message,
            application=application,
            images=request.images,
            tool_allowlist=resolved_tool_allowlist,
            reminder_ids=request.reminder_ids,
            identity=contract.principal,
            harness_release=contract.harness_release,
            sandbox=contract.sandbox,
            deadline_at=deadline_at,
            agent_contract=contract,
            metadata={
                "gateway_request_id": request.context.request_id,
                "gateway_trace_id": request.context.trace_id,
                "session_id": request.session_id,
                "agent_definition": route.agent_definition,
                "gateway_route": route.name,
                **dict(route.metadata),
                **dict(request.metadata),
                "agent_contract_id": contract.contract_id,
            },
        )
        executor = _QuotaReleasingExecutor(
            route.executor,
            self.quota,
            request.context.tenant_id,
        )
        try:
            handle = self.runtime.submit(task, executor)
        except Exception:
            executor.release()
            raise
        task_id = handle.task_id
        self._audit(
            request,
            "agent.submit",
            route_decision,
            {
                "task_id": task_id,
                "turn_id": turn_id,
                "route": route.name,
                "agent_definition": route.agent_definition,
            },
        )
        return AgentGatewayResult(
            accepted=True,
            task_id=task_id,
            route=route.name,
            handle=handle,
            decision=route_decision,
            metadata={
                "turn_id": turn_id,
                "agent_definition": route.agent_definition,
                "session_id": request.session_id,
            },
        )

    def inspect(self, task_id: str) -> RuntimeTaskState:
        return self.runtime.inspect(task_id)

    async def cancel(self, task_id: str) -> bool:
        return await self.runtime.cancel(task_id)

    async def pause(self, task_id: str) -> bool:
        return await self.runtime.pause(task_id)

    async def resume(self, task_id: str) -> bool:
        return await self.runtime.resume(task_id)

    def _resolve_route(
        self,
        request: AgentGatewayRequest,
    ) -> Optional[AgentRoute]:
        keys = (
            request.route_key,
            request.agent_definition,
            "*",
        )
        for key in keys:
            if not key:
                continue
            route = self.routes.get(key)
            if route is not None:
                return route
        if self.default_executor is not None:
            name = request.route_key or request.agent_definition or "default"
            return AgentRoute(
                name=name,
                executor=self.default_executor,
                agent_definition=(
                    request.agent_definition
                    or request.route_key
                    or "default"
                ),
            )
        return None

    def _policy_reason(
        self,
        request: AgentGatewayRequest,
        route: AgentRoute,
    ) -> str:
        context_allowed = _string_values(
            request.context.metadata.get("agent_allowlist"))
        candidates = {
            route.name,
            route.agent_definition,
            request.agent_definition,
            request.route_key,
        }
        candidates.discard("")
        if context_allowed and not candidates.intersection(context_allowed):
            return "agent 不在租户允许列表"
        if (
            self.allowed_agents is not None
            and not candidates.intersection(self.allowed_agents)
        ):
            return "agent 不在 gateway 允许列表"
        return ""

    def _reserve(
        self,
        request: AgentGatewayRequest,
        budget: TaskBudget,
    ) -> GatewayDecision:
        if self.quota is None:
            return GatewayDecision(
                layer=GatewayLayer.AGENT,
                allowed=True,
                route=f"tenant:{request.context.tenant_id}",
            )
        reserve = getattr(self.quota, "reserve", None)
        if not callable(reserve):
            return GatewayDecision(
                layer=GatewayLayer.AGENT,
                allowed=True,
                route="quota:unavailable",
            )
        try:
            decision = reserve(
                request.context.tenant_id,
                # Task budget is enforced by the Harness. Agent admission is
                # task-granular and must not charge the LLM token window.
                tokens=0,
                cost_usd=max(0.0, float(budget.max_cost_usd or 0.0)),
                layer=GatewayLayer.AGENT,
            )
        except TypeError:
            decision = reserve(
                request.context.tenant_id,
                tokens=0,
                cost_usd=max(0.0, float(budget.max_cost_usd or 0.0)),
            )
        return normalize_quota_decision(decision, GatewayLayer.AGENT)

    def _audit(
        self,
        request: AgentGatewayRequest,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None:
        self.audit.record(
            GatewayLayer.AGENT,
            request.context,
            action,
            decision,
            metadata,
        )


def _deadline(*timeouts: Optional[float]) -> Optional[float]:
    resolved = [
        float(value) for value in timeouts
        if value is not None and float(value) > 0
    ]
    if not resolved:
        return None
    return time.time() + min(resolved)


def _string_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if (
        isinstance(value, (Sequence, Set))
        and not isinstance(value, (bytes, bytearray, str))
    ):
        return tuple(str(item) for item in value if str(item))
    return ()


__all__ = [
    "AgentGateway",
    "AgentGatewayError",
    "AgentRoute",
]
