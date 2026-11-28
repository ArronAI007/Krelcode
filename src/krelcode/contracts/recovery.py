"""Failure classification and bounded recovery contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class FailureClass(str, Enum):
    TRANSIENT_PROVIDER = "transient_provider"
    INVALID_ARGUMENTS = "invalid_arguments"
    MISSING_ENVIRONMENT = "missing_environment"
    POLICY_DENIED = "policy_denied"
    PERMISSION_REQUIRED = "permission_required"
    REPEATED_EXPLORATION = "repeated_exploration"
    NO_PROGRESS = "no_progress"
    SIDE_EFFECT_UNKNOWN = "side_effect_unknown"
    BUSINESS_GAP = "business_gap"
    ACCEPTANCE_FAILED = "acceptance_failed"
    INTERNAL = "internal"


class Repairability(str, Enum):
    AUTO_RETRY = "auto_retry"
    REPAIR_AND_RETRY = "repair_and_retry"
    REPLAN = "replan"
    WAIT = "wait"
    USER_ACTION = "user_action"
    UNRECOVERABLE = "unrecoverable"


class RecoveryStrategy(str, Enum):
    BOUNDED_BACKOFF = "bounded_backoff"
    FIX_ARGUMENTS = "fix_arguments"
    REBUILD_ENVIRONMENT = "rebuild_environment"
    WAIT_FOR_PERMISSION = "wait_for_permission"
    REPLAN = "replan"
    NARROW_SCOPE = "narrow_scope"
    QUERY_SIDE_EFFECT = "query_side_effect"
    DELEGATE_VERIFICATION = "delegate_verification"
    ESCALATE = "escalate"
    FAIL = "fail"


@dataclass(frozen=True)
class RecoveryDecision:
    failure_class: FailureClass | str
    strategy: RecoveryStrategy | str
    repairability: Repairability | str
    reason: str
    retryable: bool = False
    requires_replan: bool = False
    requires_user: bool = False
    attempt: int = 0
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "failure_class", FailureClass(self.failure_class))
        object.__setattr__(
            self, "strategy", RecoveryStrategy(self.strategy))
        object.__setattr__(
            self, "repairability", Repairability(self.repairability))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        if not self.reason:
            raise ValueError("RecoveryDecision.reason 不能为空")
        object.__setattr__(self, "retryable", bool(self.retryable))
        object.__setattr__(
            self, "requires_replan", bool(self.requires_replan))
        object.__setattr__(
            self, "requires_user", bool(self.requires_user))
        object.__setattr__(self, "attempt", max(0, int(self.attempt)))
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_class": FailureClass(self.failure_class).value,
            "strategy": RecoveryStrategy(self.strategy).value,
            "repairability": Repairability(self.repairability).value,
            "reason": self.reason,
            "retryable": self.retryable,
            "requires_replan": self.requires_replan,
            "requires_user": self.requires_user,
            "attempt": self.attempt,
            "evidence": dict(self.evidence),
        }


__all__ = [
    "FailureClass",
    "RecoveryDecision",
    "RecoveryStrategy",
    "Repairability",
]
