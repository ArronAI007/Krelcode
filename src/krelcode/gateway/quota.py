"""In-memory tenant quota implementation for gateway admission."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from krelcode.contracts.gateway import (
    GatewayDecision,
    GatewayLayer,
    TenantQuota,
    TenantQuotaPort,
)


@dataclass(frozen=True)
class _UsageSample:
    at: float
    requests: int
    tokens: int
    cost_usd: float


@dataclass
class _TenantWindow:
    active: int = 0
    samples: deque[_UsageSample] | None = None

    def __post_init__(self) -> None:
        if self.samples is None:
            self.samples = deque()


class InMemoryTenantQuota(TenantQuotaPort):
    """Bounded per-tenant concurrency, request, token and cost admission.

    One instance can be shared by a gateway plane.  Separate LLM and MCP
    gateway instances should receive separate quota instances so a tool call
    cannot consume a model-token budget.
    """

    def __init__(
        self,
        quota: TenantQuota | None = None,
        *,
        window_seconds: float = 60.0,
        clock: object | None = None,
    ) -> None:
        self.quota = quota or TenantQuota()
        self.window_seconds = max(1.0, float(window_seconds))
        self._clock = clock
        self._windows: dict[tuple[str, GatewayLayer], _TenantWindow] = {}
        self._lock = threading.RLock()

    def reserve(
        self,
        tenant_id: str,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        layer: GatewayLayer | str = GatewayLayer.LLM,
    ) -> GatewayDecision:
        tenant = str(tenant_id or "local").strip() or "local"
        requested_tokens = max(0, int(tokens))
        requested_cost = max(0.0, float(cost_usd))
        resolved_layer = GatewayLayer(layer)
        now = self._now()
        with self._lock:
            window = self._windows.setdefault(
                (tenant, resolved_layer), _TenantWindow())
            samples = window.samples
            assert samples is not None
            self._prune(samples, now)
            requests = sum(item.requests for item in samples)
            used_tokens = sum(item.tokens for item in samples)
            if window.active >= self.quota.max_concurrent:
                return self._deny(
                    resolved_layer,
                    tenant,
                    "租户并发配额已耗尽",
                    remaining=0,
                )
            if requests + 1 > self.quota.max_requests_per_minute:
                return self._deny(
                    resolved_layer,
                    tenant,
                    "租户请求速率配额已耗尽",
                    remaining=0,
                )
            if used_tokens + requested_tokens > (
                self.quota.max_tokens_per_minute
            ):
                return self._deny(
                    resolved_layer,
                    tenant,
                    "租户 token 速率配额已耗尽",
                    remaining=max(
                        0,
                        self.quota.max_tokens_per_minute - used_tokens,
                    ),
                )
            if (
                requested_cost > 0
                and self.quota.max_cost_usd_per_task is not None
                and requested_cost > self.quota.max_cost_usd_per_task
            ):
                return self._deny(
                    resolved_layer,
                    tenant,
                    "单任务成本预算已耗尽",
                    remaining=None,
                )
            window.active += 1
            samples.append(_UsageSample(
                at=now,
                requests=1,
                tokens=requested_tokens,
                cost_usd=requested_cost,
            ))
            remaining = min(
                max(0, self.quota.max_requests_per_minute - requests - 1),
                max(
                    0,
                    self.quota.max_tokens_per_minute
                    - used_tokens
                    - requested_tokens,
                ),
            )
            return GatewayDecision(
                layer=resolved_layer,
                allowed=True,
                route=f"tenant:{tenant}",
                quota_remaining=remaining,
                metadata={
                    "tenant_id": tenant,
                    "active": window.active,
                    "max_concurrent": self.quota.max_concurrent,
                    "requests_in_window": requests + 1,
                    "tokens_in_window": used_tokens + requested_tokens,
                },
            )

    def release(
        self,
        tenant_id: str,
        *,
        layer: GatewayLayer | str = GatewayLayer.LLM,
    ) -> None:
        tenant = str(tenant_id or "local").strip() or "local"
        resolved_layer = GatewayLayer(layer)
        with self._lock:
            window = self._windows.get((tenant, resolved_layer))
            if window is not None and window.active > 0:
                window.active -= 1

    def snapshot(
        self,
        tenant_id: str,
        *,
        layer: GatewayLayer | str = GatewayLayer.LLM,
    ) -> dict[str, object]:
        tenant = str(tenant_id or "local").strip() or "local"
        resolved_layer = GatewayLayer(layer)
        now = self._now()
        with self._lock:
            window = self._windows.setdefault(
                (tenant, resolved_layer), _TenantWindow())
            samples = window.samples
            assert samples is not None
            self._prune(samples, now)
            return {
                "tenant_id": tenant,
                "layer": resolved_layer.value,
                "active": window.active,
                "requests_in_window": sum(
                    item.requests for item in samples),
                "tokens_in_window": sum(item.tokens for item in samples),
                "cost_in_window": sum(item.cost_usd for item in samples),
            }

    def _prune(
        self,
        samples: deque[_UsageSample],
        now: float,
    ) -> None:
        cutoff = now - self.window_seconds
        while samples and samples[0].at < cutoff:
            samples.popleft()

    def _now(self) -> float:
        if callable(self._clock):
            value = self._clock()
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
        return time.monotonic()

    @staticmethod
    def _deny(
        layer: GatewayLayer,
        tenant: str,
        reason: str,
        *,
        remaining: int | None,
    ) -> GatewayDecision:
        return GatewayDecision(
            layer=layer,
            allowed=False,
            reason=reason,
            route=f"tenant:{tenant}",
            quota_remaining=remaining,
            metadata={"tenant_id": tenant},
        )


def normalize_quota_decision(
    decision: object,
    layer: GatewayLayer,
) -> GatewayDecision:
    if not isinstance(decision, GatewayDecision):
        return GatewayDecision(
            layer=layer,
            allowed=False,
            reason="quota port 返回非法 decision",
        )
    return GatewayDecision(
        layer=layer,
        allowed=decision.allowed,
        reason=decision.reason,
        route=decision.route,
        quota_remaining=decision.quota_remaining,
        metadata=decision.metadata,
    )


__all__ = ["InMemoryTenantQuota", "normalize_quota_decision"]
