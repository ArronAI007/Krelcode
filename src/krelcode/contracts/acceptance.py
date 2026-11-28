"""Runtime acceptance-evaluation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, Optional, Protocol, Tuple

from .application import AcceptanceContract, ApplicationRequest
from .runtime import ArtifactIdentity

if TYPE_CHECKING:
    from .agent import AgentContract


class AcceptanceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class AcceptanceEvidence:
    kind: str
    source: str
    passed: bool
    summary: str = ""
    artifact: ArtifactIdentity | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    deliverable_id: str = ""
    digest: str = ""

    def __post_init__(self) -> None:
        for name in ("kind", "source", "summary"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(
            self, "deliverable_id",
            str(self.deliverable_id or "").strip())
        object.__setattr__(self, "digest", str(self.digest or "").strip())


@dataclass(frozen=True)
class AcceptanceCheck:
    criterion: str
    passed: bool
    reason: str = ""
    evidence: Tuple[AcceptanceEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "criterion", str(self.criterion or "").strip())
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class AcceptanceResult:
    status: AcceptanceStatus
    request_goal: str
    contract: AcceptanceContract
    checks: Tuple[AcceptanceCheck, ...] = ()
    evidence: Tuple[AcceptanceEvidence, ...] = ()
    reason: str = ""
    final_response_hash: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AcceptanceStatus(self.status))
        object.__setattr__(
            self, "request_goal", str(self.request_goal or "").strip())
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self, "final_response_hash",
            str(self.final_response_hash or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def passed(self) -> bool:
        return self.status is AcceptanceStatus.PASSED

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "request_goal": self.request_goal,
            "criteria": list(self.contract.criteria),
            "required_evidence": list(self.contract.required_evidence),
            "checks": [
                {
                    "criterion": check.criterion,
                    "passed": check.passed,
                    "reason": check.reason,
                    "evidence_count": len(check.evidence),
                }
                for check in self.checks
            ],
            "evidence": [
                {
                    "kind": item.kind,
                    "source": item.source,
                    "passed": item.passed,
                    "summary": item.summary,
                    "deliverable_id": item.deliverable_id,
                    "digest": item.digest,
                    "artifact_id": (
                        item.artifact.artifact_id
                        if item.artifact is not None else ""),
                }
                for item in self.evidence
            ],
            "reason": self.reason,
            "final_response_hash": self.final_response_hash,
            "metadata": dict(self.metadata),
        }


class AcceptanceEvaluatorPort(Protocol):
    def evaluate(
        self,
        request: ApplicationRequest,
        final_response: str,
        evidence: Tuple[AcceptanceEvidence, ...],
        contract: Optional["AgentContract"] = None,
    ) -> AcceptanceResult: ...


__all__ = [
    "AcceptanceCheck",
    "AcceptanceEvaluatorPort",
    "AcceptanceEvidence",
    "AcceptanceResult",
    "AcceptanceStatus",
]
