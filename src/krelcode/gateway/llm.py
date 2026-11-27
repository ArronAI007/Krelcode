"""LLM gateway: model-call routing, tenant quotas and provider fallback."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import AsyncIterator, Optional, Protocol, cast

from krelcode.contracts.application import TaskBudget
from krelcode.contracts.gateway import (
    GatewayAuditPort,
    GatewayContext,
    GatewayDecision,
    GatewayLayer,
    LLMGatewayRequest,
    LLMGatewayRoute,
    TenantQuota,
    TenantQuotaPort,
)
from krelcode.core.messages import Message
from krelcode.core.tokens import estimate_text_tokens
from krelcode.llm.base import (
    EvidenceChannel,
    LLMEvent,
    LLMProvider,
    PreparedLLMRequest,
    ProviderCapabilities,
    StreamFinished,
    ToolSpec,
    supports_keyword_argument,
)
from .audit import NullGatewayAudit
from .quota import InMemoryTenantQuota, normalize_quota_decision


class LLMGatewayError(RuntimeError):
    """Base error for model gateway execution."""


class LLMGatewayDenied(LLMGatewayError):
    """A model call was rejected by gateway admission policy."""


class _PreparedProvider(Protocol):
    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
    ) -> PreparedLLMRequest: ...

    def stream_prepared(
        self,
        prepared: PreparedLLMRequest,
    ) -> AsyncIterator[LLMEvent]: ...


class LLMGateway:
    """Provider-compatible gateway with explicit model-call boundaries.

    The class implements the existing ``LLMProvider`` surface so AgentLoop can
    consume it without a second execution path.  Quota and audit decisions are
    scoped to this one model call; MCP ACLs and task budgets remain in their own
    gateway planes.
    """

    def __init__(
        self,
        providers: Mapping[str, LLMProvider] | LLMProvider,
        *,
        default_provider: str = "",
        routes: Optional[Mapping[str, str]] = None,
        fallback_providers: (
            Mapping[str, Sequence[str]] | Sequence[str] | None
        ) = None,
        quota: Optional[TenantQuotaPort] = None,
        quota_policy: Optional[TenantQuota] = None,
        audit: Optional[GatewayAuditPort] = None,
        context: Optional[GatewayContext] = None,
    ) -> None:
        if isinstance(providers, Mapping):
            if not providers:
                raise ValueError("LLMGateway 至少需要一个 provider")
            self.providers = dict(providers)
            resolved_default = str(default_provider or "").strip()
        else:
            name = str(getattr(providers, "name", "") or "default")
            self.providers = {name: providers}
            resolved_default = str(default_provider or name).strip()
        if resolved_default not in self.providers:
            if default_provider:
                raise ValueError(f"未知默认 provider: {resolved_default}")
            resolved_default = sorted(self.providers)[0]
        self.default_provider = resolved_default
        self.routes = {
            str(key): str(value)
            for key, value in (routes or {}).items()
            if str(key) and str(value)
        }
        self._fallback_config = fallback_providers
        self.quota = quota
        if self.quota is None and quota_policy is not None:
            self.quota = InMemoryTenantQuota(quota_policy)
        self.audit = audit or NullGatewayAudit()
        self.context = context or GatewayContext(
            request_id=f"llm-{uuid.uuid4().hex}",
            budget=TaskBudget(),
        )
        self._prepared_fallbacks: dict[
            str, dict[str, PreparedLLMRequest]] = {}

    @property
    def name(self) -> str:
        return self._primary.name

    @property
    def model(self) -> str:
        return self._primary.model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._primary.capabilities

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.providers))

    @property
    def _primary(self) -> LLMProvider:
        return self.providers[self.default_provider]

    def set_context(self, context: GatewayContext) -> None:
        self.context = context

    def route(self, request: LLMGatewayRequest) -> LLMGatewayRoute:
        provider_name = (
            request.provider
            or self.routes.get(request.model)
            or self.routes.get("*")
            or self.default_provider
        )
        provider = self.providers.get(provider_name)
        if provider is None:
            decision = GatewayDecision(
                layer=GatewayLayer.LLM,
                allowed=False,
                reason=f"未知 provider: {provider_name}",
                route=provider_name,
            )
            self._audit(
                request.context,
                "llm.route",
                decision,
                {"provider": provider_name, "model": request.model},
            )
            return LLMGatewayRoute(
                provider_name=provider_name,
                model=request.model,
                decision=decision,
            )
        model = request.model or provider.model
        fallbacks = tuple(
            name for name in self._fallbacks_for(provider_name)
            if name != provider_name and name in self.providers
        )
        decision = GatewayDecision(
            layer=GatewayLayer.LLM,
            allowed=True,
            route=f"{provider_name}/{model}",
            metadata={
                "provider": provider_name,
                "model": model,
                "fallback_providers": list(fallbacks),
            },
        )
        self._audit(
            request.context,
            "llm.route",
            decision,
            {"provider": provider_name, "model": model},
        )
        return LLMGatewayRoute(
            provider_name=provider_name,
            model=model,
            fallback_providers=fallbacks,
            decision=decision,
        )

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
        context: Optional[GatewayContext] = None,
    ) -> AsyncIterator[LLMEvent]:
        if not messages:
            raise LLMGatewayError("LLM 请求不能没有消息")
        resolved_context = context or self.context
        request = LLMGatewayRequest(
            context=resolved_context,
            model=str(
                resolved_context.metadata.get("model")
                or getattr(self._primary, "model", "")
                or ""
            ),
            provider=str(
                resolved_context.metadata.get("provider") or ""),
            estimated_input_tokens=_estimate_input_tokens(messages, tools),
        )
        route = self.route(request)
        decision = route.decision or GatewayDecision(
            layer=GatewayLayer.LLM,
            allowed=False,
            reason="gateway route 缺失 decision",
        )
        if not decision.allowed:
            raise LLMGatewayDenied(decision.reason or "LLM gateway 拒绝请求")
        reserved = self._reserve(resolved_context, request)
        if not reserved.allowed:
            self._audit(
                resolved_context,
                "llm.admit",
                reserved,
                {"provider": route.provider_name, "model": route.model},
            )
            raise LLMGatewayDenied(reserved.reason or "LLM 配额拒绝")
        started_at = time.monotonic()
        usage: dict[str, int] = {}
        try:
            candidates = (
                route.provider_name,
                *route.fallback_providers,
            )
            last_error: Optional[BaseException] = None
            for index, provider_name in enumerate(candidates):
                provider = self.providers.get(provider_name)
                if provider is None:
                    continue
                emitted = False
                try:
                    stream_call = (
                        provider.stream(
                            messages, tools, evidence=evidence)
                        if supports_keyword_argument(
                            provider.stream, "evidence")
                        else provider.stream(messages, tools)
                    )
                    async for event in stream_call:
                        emitted = True
                        if isinstance(event, StreamFinished):
                            usage = dict(event.usage)
                        yield event
                    self._audit(
                        resolved_context,
                        "llm.complete",
                        decision,
                        {
                            "provider": provider_name,
                            "model": route.model,
                            "fallback_index": index,
                            "duration_ms": int(
                                (time.monotonic() - started_at) * 1000),
                            "input_tokens": int(
                                usage.get("input_tokens", 0)),
                            "output_tokens": int(
                                usage.get("output_tokens", 0)),
                        },
                    )
                    return
                except Exception as exc:
                    last_error = exc
                    if emitted or index == len(candidates) - 1:
                        self._audit(
                            resolved_context,
                            "llm.error",
                            decision,
                            {
                                "provider": provider_name,
                                "model": route.model,
                                "error_type": type(exc).__name__,
                                "emitted_before_error": emitted,
                            },
                        )
                        raise
                    self._audit(
                        resolved_context,
                        "llm.fallback",
                        decision,
                        {
                            "provider": provider_name,
                            "error_type": type(exc).__name__,
                            "next_provider": candidates[index + 1],
                        },
                    )
            raise LLMGatewayError(
                f"所有 LLM provider 均不可用: "
                f"{type(last_error).__name__ if last_error else 'unknown'}"
            ) from last_error
        finally:
            if self.quota is not None:
                _release_quota(
                    self.quota, resolved_context.tenant_id)

    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
        context: Optional[GatewayContext] = None,
    ) -> PreparedLLMRequest:
        resolved_context = context or self.context
        route = self.route(LLMGatewayRequest(
            context=resolved_context,
            model=str(resolved_context.metadata.get("model") or ""),
            provider=str(
                resolved_context.metadata.get("provider") or ""),
            estimated_input_tokens=_estimate_input_tokens(messages, tools),
        ))
        decision = route.decision
        if decision is None or not decision.allowed:
            raise LLMGatewayDenied(
                decision.reason if decision is not None else "LLM 路由失败")
        prepared_by_provider: dict[str, PreparedLLMRequest] = {}
        for provider_name in (
            route.provider_name,
            *route.fallback_providers,
        ):
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            try:
                prepared = self._prepare_provider(
                    provider, messages, tools, evidence)
            except LLMGatewayError:
                if provider_name == route.provider_name:
                    raise
                continue
            prepared_by_provider[provider_name] = prepared
        primary = prepared_by_provider.get(route.provider_name)
        if primary is None:
            raise LLMGatewayError(
                f"provider 不支持 prepare_request: {route.provider_name}")
        self._prepared_fallbacks[primary.payload_digest] = (
            prepared_by_provider)
        return primary

    async def stream_prepared(
        self,
        prepared: PreparedLLMRequest,
    ) -> AsyncIterator[LLMEvent]:
        context = self.context
        reserve_request = LLMGatewayRequest(
            context=context,
            provider=prepared.provider,
            model=prepared.model,
            estimated_input_tokens=prepared.request_body_tokens,
        )
        reserved = self._reserve(context, reserve_request)
        if not reserved.allowed:
            self._audit(
                context,
                "llm.admit",
                reserved,
                {
                    "provider": prepared.provider,
                    "model": prepared.model,
                },
            )
            raise LLMGatewayDenied(reserved.reason or "LLM 配额拒绝")
        candidates = self._prepared_fallbacks.get(
            prepared.payload_digest,
            {prepared.provider: prepared},
        )
        ordered = dict(candidates)
        ordered.setdefault(prepared.provider, prepared)
        try:
            last_error: Optional[BaseException] = None
            names = tuple(ordered)
            for index, provider_name in enumerate(names):
                provider = self.providers.get(provider_name)
                candidate = ordered[provider_name]
                if provider is None:
                    continue
                stream_prepared = getattr(
                    provider, "stream_prepared", None)
                if not callable(stream_prepared):
                    continue
                emitted = False
                try:
                    async for event in stream_prepared(candidate):
                        emitted = True
                        yield event
                    self._audit(
                        context,
                        "llm.complete",
                        reserved,
                        {
                            "provider": provider_name,
                            "model": candidate.model,
                            "fallback_index": index,
                            "prepared_digest": candidate.payload_digest,
                        },
                    )
                    return
                except Exception as exc:
                    last_error = exc
                    if emitted or index == len(names) - 1:
                        raise
                    self._audit(
                        context,
                        "llm.fallback",
                        reserved,
                        {
                            "provider": provider_name,
                            "error_type": type(exc).__name__,
                            "next_provider": names[index + 1],
                        },
                    )
            raise LLMGatewayError(
                "所有 prepared LLM provider 均不可用"
            ) from last_error
        finally:
            self._prepared_fallbacks.pop(prepared.payload_digest, None)
            if self.quota is not None:
                _release_quota(self.quota, context.tenant_id)

    def _prepare_provider(
        self,
        provider: LLMProvider,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]],
        evidence: Optional[EvidenceChannel],
    ) -> PreparedLLMRequest:
        prepare = getattr(provider, "prepare_request", None)
        if not callable(prepare):
            raise LLMGatewayError(
                f"provider 不支持 prepare_request: "
                f"{getattr(provider, 'name', '?')}")
        prepared_provider = cast(_PreparedProvider, provider)
        if supports_keyword_argument(
            prepared_provider.prepare_request, "evidence"
        ):
            return prepared_provider.prepare_request(
                messages,
                tools,
                evidence=evidence,
            )
        return prepared_provider.prepare_request(messages, tools)

    def _reserve(
        self,
        context: GatewayContext,
        request: LLMGatewayRequest,
    ) -> GatewayDecision:
        if self.quota is None:
            return GatewayDecision(
                layer=GatewayLayer.LLM,
                allowed=True,
                route="quota:disabled",
            )
        reserve = getattr(self.quota, "reserve", None)
        if not callable(reserve):
            return GatewayDecision(
                layer=GatewayLayer.LLM,
                allowed=True,
                route="quota:unavailable",
            )
        try:
            decision = reserve(
                context.tenant_id,
                tokens=(
                    request.estimated_input_tokens
                    + request.estimated_output_tokens
                ),
                cost_usd=request.estimated_cost_usd,
                layer=GatewayLayer.LLM,
            )
        except TypeError:
            decision = reserve(
                context.tenant_id,
                tokens=(
                    request.estimated_input_tokens
                    + request.estimated_output_tokens
                ),
                cost_usd=request.estimated_cost_usd,
            )
        return normalize_quota_decision(decision, GatewayLayer.LLM)

    def _fallbacks_for(self, provider_name: str) -> tuple[str, ...]:
        raw = self._fallback_config
        if isinstance(raw, Mapping):
            values = raw.get(provider_name, ())
            if isinstance(values, str):
                return (values,)
            return tuple(str(item) for item in values)
        if isinstance(raw, str):
            return (raw,)
        return tuple(str(item) for item in (raw or ()))

    def _audit(
        self,
        context: GatewayContext,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None:
        self.audit.record(
            GatewayLayer.LLM,
            context,
            action,
            decision,
            metadata,
        )


def _estimate_input_tokens(
    messages: Sequence[Message],
    tools: Optional[Sequence[ToolSpec]],
) -> int:
    text_chars = sum(
        estimate_text_tokens(message.content or "")
        for message in messages
    )
    return text_chars + estimate_text_tokens(
        json.dumps(
            list(tools or ()),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    )


def _release_quota(
    quota: TenantQuotaPort,
    tenant_id: str,
) -> None:
    release = getattr(quota, "release", None)
    if not callable(release):
        return
    try:
        release(tenant_id, layer=GatewayLayer.LLM)
    except TypeError:
        release(tenant_id)


__all__ = [
    "LLMGateway",
    "LLMGatewayDenied",
    "LLMGatewayError",
]
