"""Content-light gateway decision audit adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from krelcode.contracts.event_log import EventLogPort
from krelcode.contracts.gateway import (
    GatewayAuditPort,
    GatewayContext,
    GatewayDecision,
    GatewayLayer,
)


@dataclass(frozen=True)
class GatewayAuditRecord:
    layer: GatewayLayer
    action: str
    request_id: str
    tenant_id: str
    principal_id: str
    allowed: bool
    reason: str
    route: str
    metadata: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "type": "gateway_decision",
            "layer": self.layer.value,
            "action": self.action,
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "route": self.route,
            "metadata": dict(self.metadata),
        }


class NullGatewayAudit(GatewayAuditPort):
    def record(
        self,
        layer: GatewayLayer,
        context: GatewayContext,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None:
        return None


class InMemoryGatewayAudit(GatewayAuditPort):
    """Bounded in-memory audit adapter for tests and local diagnostics."""

    def __init__(self, limit: int = 10_000) -> None:
        self.limit = max(1, int(limit))
        self.records: list[GatewayAuditRecord] = []

    def record(
        self,
        layer: GatewayLayer,
        context: GatewayContext,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None:
        self.records.append(GatewayAuditRecord(
            layer=GatewayLayer(layer),
            action=str(action or ""),
            request_id=context.request_id,
            tenant_id=context.tenant_id,
            principal_id=context.principal_id,
            allowed=decision.allowed,
            reason=decision.reason,
            route=decision.route,
            metadata=dict(metadata),
        ))
        if len(self.records) > self.limit:
            del self.records[:len(self.records) - self.limit]


class EventLogGatewayAudit(GatewayAuditPort):
    """Persist gateway decisions in the authoritative append-only event log."""

    def __init__(
        self,
        event_log: EventLogPort,
        *,
        fallback: Optional[GatewayAuditPort] = None,
    ) -> None:
        self.event_log = event_log
        self.fallback = fallback

    def record(
        self,
        layer: GatewayLayer,
        context: GatewayContext,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None:
        record = GatewayAuditRecord(
            layer=GatewayLayer(layer),
            action=str(action or ""),
            request_id=context.request_id,
            tenant_id=context.tenant_id,
            principal_id=context.principal_id,
            allowed=decision.allowed,
            reason=decision.reason,
            route=decision.route,
            metadata=dict(metadata),
        )
        try:
            self.event_log.append(
                record.as_dict(),
                event_type="gateway_decision",
                session_id=context.session_id,
                turn_id=context.turn_id,
            )
        except (OSError, TypeError, ValueError):
            if self.fallback is not None:
                self.fallback.record(
                    layer, context, action, decision, metadata)


class CompositeGatewayAudit(GatewayAuditPort):
    def __init__(self, *sinks: GatewayAuditPort) -> None:
        self.sinks = tuple(sinks)

    def record(
        self,
        layer: GatewayLayer,
        context: GatewayContext,
        action: str,
        decision: GatewayDecision,
        metadata: Mapping[str, object],
    ) -> None:
        for sink in self.sinks:
            sink.record(layer, context, action, decision, metadata)


__all__ = [
    "CompositeGatewayAudit",
    "EventLogGatewayAudit",
    "GatewayAuditRecord",
    "InMemoryGatewayAudit",
    "NullGatewayAudit",
]
