"""Explicit Agent Loop phases, observations, waiting and verification gaps."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

from .budget import UnfinishedManifest


class LoopPhase(str, Enum):
    PREPARE = "prepare"
    MODEL = "model"
    ACT = "act"
    OBSERVE = "observe"
    VERIFY = "verify"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class LoopTerminationReason(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_STEPS = "max_steps"
    LOOP_DETECTED = "loop_detected"
    NO_PROGRESS = "no_progress"
    PROTOCOL_ERROR = "protocol_error"
    ACCEPTANCE_FAILED = "acceptance_failed"
    REVIEW_REQUIRED = "review_required"
    POLICY_DENIED = "policy_denied"
    PARTIAL_DELIVERY = "partial_delivery"


class ObservationSource(str, Enum):
    TOOL = "tool"
    USER = "user"
    USER_INPUT = "user_input"
    SUBAGENT = "subagent"
    JOB = "job"
    MCP = "mcp"
    HOOK = "hook"
    SYSTEM = "system"


class ObservationStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    PENDING = "pending"


class WaitKind(str, Enum):
    APPROVAL = "approval"
    USER_INPUT = "user_input"
    SUBAGENT = "subagent"
    JOB = "job"
    EXTERNAL_EVENT = "external_event"
    REVIEW = "review"
    BUDGET_DECISION = "budget_decision"


@dataclass(frozen=True)
class Observation:
    observation_id: str
    turn_id: str
    source: ObservationSource | str
    kind: str
    status: ObservationStatus | str = ObservationStatus.OK
    summary: str = ""
    data: Mapping[str, object] = field(default_factory=dict)
    trust: str = "trusted"
    requires_validation: bool = False
    evidence_ids: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("observation_id", "turn_id", "kind", "summary", "trust"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.observation_id:
            raise ValueError("Observation.observation_id 不能为空")
        object.__setattr__(self, "source", ObservationSource(self.source))
        object.__setattr__(self, "status", ObservationStatus(self.status))
        object.__setattr__(
            self, "requires_validation", bool(self.requires_validation))
        object.__setattr__(
            self,
            "evidence_ids",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.evidence_ids
                if str(item).strip()
            )),
        )
        object.__setattr__(
            self, "data", MappingProxyType(dict(self.data)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def digest(self) -> str:
        return _digest({
            "source": ObservationSource(self.source).value,
            "kind": self.kind,
            "status": ObservationStatus(self.status).value,
            "summary": self.summary,
            "data": dict(self.data),
            "trust": self.trust,
            "requires_validation": self.requires_validation,
            "evidence_ids": list(self.evidence_ids),
        })

    def as_record(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "turn_id": self.turn_id,
            "source": ObservationSource(self.source).value,
            "kind": self.kind,
            "status": ObservationStatus(self.status).value,
            "summary": self.summary,
            "data": dict(self.data),
            "trust": self.trust,
            "requires_validation": self.requires_validation,
            "evidence_ids": list(self.evidence_ids),
            "digest": self.digest,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class WaitCondition:
    kind: WaitKind | str
    request_id: str
    reason: str
    resume_token: str = ""
    deadline_at: Optional[float] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", WaitKind(self.kind))
        object.__setattr__(
            self, "request_id", str(self.request_id or "").strip())
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self, "resume_token", str(self.resume_token or "").strip())
        if self.deadline_at is not None:
            object.__setattr__(
                self, "deadline_at", float(self.deadline_at))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_record(self) -> dict[str, object]:
        return {
            "kind": WaitKind(self.kind).value,
            "request_id": self.request_id,
            "reason": self.reason,
            "resume_token": self.resume_token,
            "deadline_at": self.deadline_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VerificationGap:
    criterion: str
    message: str
    retryable: bool = True
    code: str = "acceptance_failed"
    observation_ids: tuple[str, ...] = ()
    suggested_action: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "criterion", "message", "code", "suggested_action",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.message:
            raise ValueError("VerificationGap.message 不能为空")
        object.__setattr__(self, "retryable", bool(self.retryable))
        object.__setattr__(
            self,
            "observation_ids",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.observation_ids
                if str(item).strip()
            )),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_record(self) -> dict[str, object]:
        return {
            "criterion": self.criterion,
            "message": self.message,
            "retryable": self.retryable,
            "code": self.code,
            "observation_ids": list(self.observation_ids),
            "suggested_action": self.suggested_action,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class LoopTransition:
    turn_id: str
    previous_phase: LoopPhase
    phase: LoopPhase
    reason: str
    step: int = 0
    observation_id: str = ""
    waiting: Optional[WaitCondition] = None
    gap: Optional[VerificationGap] = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "previous_phase", LoopPhase(self.previous_phase))
        object.__setattr__(self, "phase", LoopPhase(self.phase))
        object.__setattr__(self, "step", max(0, int(self.step)))
        object.__setattr__(
            self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self,
            "observation_id",
            str(self.observation_id or "").strip(),
        )
        object.__setattr__(self, "turn_id", str(self.turn_id or "").strip())

    def as_record(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "previous_phase": self.previous_phase.value,
            "phase": self.phase.value,
            "reason": self.reason,
            "step": self.step,
            "observation_id": self.observation_id,
            "waiting": (
                self.waiting.as_record() if self.waiting is not None else None),
            "gap": self.gap.as_record() if self.gap is not None else None,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LoopTermination:
    reason: LoopTerminationReason | str
    final_text: str = ""
    error: str = ""
    turn_id: str = ""
    phase: LoopPhase | str = LoopPhase.FAILED
    metadata: Mapping[str, object] = field(default_factory=dict)
    unfinished: Optional[UnfinishedManifest] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", LoopTerminationReason(self.reason))
        object.__setattr__(self, "phase", LoopPhase(self.phase))
        object.__setattr__(
            self, "final_text", str(self.final_text or ""))
        object.__setattr__(self, "error", str(self.error or "").strip())
        object.__setattr__(self, "turn_id", str(self.turn_id or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def succeeded(self) -> bool:
        return LoopTerminationReason(self.reason) is (
            LoopTerminationReason.COMPLETED)

    def as_record(self) -> dict[str, object]:
        return {
            "reason": LoopTerminationReason(self.reason).value,
            "final_text": self.final_text,
            "error": self.error,
            "turn_id": self.turn_id,
            "phase": LoopPhase(self.phase).value,
            "metadata": dict(self.metadata),
            "unfinished": (
                self.unfinished.as_record()
                if self.unfinished is not None else None),
        }


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "LoopPhase",
    "LoopTermination",
    "LoopTerminationReason",
    "LoopTransition",
    "Observation",
    "ObservationSource",
    "ObservationStatus",
    "VerificationGap",
    "WaitCondition",
    "WaitKind",
]
