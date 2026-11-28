"""Encoding helpers for loop runtime control events."""

from __future__ import annotations

from typing import Any, Dict

from ..core.events import MiddlewareEvaluated, RecoveryDecided


def middleware_payload(event: MiddlewareEvaluated) -> Dict[str, Any]:
    return {
        "type": "middleware",
        "subtype": "evaluated",
        "lifecycle_point": event.lifecycle_point,
        "action": event.action,
        "reason": event.reason,
        "evaluations": [dict(item) for item in event.evaluations],
        "applied_patches": [dict(item) for item in event.applied_patches],
    }


def recovery_payload(event: RecoveryDecided) -> Dict[str, Any]:
    return {
        "type": "recovery",
        "subtype": "decided",
        "failure_class": event.failure_class,
        "strategy": event.strategy,
        "reason": event.reason,
        "retryable": event.retryable,
        "requires_replan": event.requires_replan,
        "requires_user": event.requires_user,
        "attempt": event.attempt,
        "evidence": dict(event.evidence),
    }


__all__ = ["middleware_payload", "recovery_payload"]
