"""Universal candidate and runtime-admission contracts for hot changes."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Tuple

from .application import RiskLevel
from .governance import GovernanceDecision


class ChangeType(str, Enum):
    PROMPT = "prompt"
    PLUGIN = "plugin"
    SKILL = "skill"
    AGENT_DEFINITION = "agent_definition"
    MODEL_PROFILE = "model_profile"
    TOOL_CATALOG = "tool_catalog"
    MCP_SERVER = "mcp_server"
    MEMORY_MIGRATION = "memory_migration"
    AUTO_DREAM = "auto_dream"


class AdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"


@dataclass(frozen=True)
class ChangeEvidence:
    kind: str
    source: str
    digest: str = ""
    summary: str = ""
    passed: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("kind", "source", "digest", "summary"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class EvaluationCheck:
    name: str
    passed: bool
    reason: str = ""
    required: bool = True
    score: float = 0.0
    threshold: float = 0.0
    evidence: Tuple[ChangeEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "threshold", float(self.threshold))
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class EvaluationReport:
    candidate_id: str
    passed: bool
    checks: Tuple[EvaluationCheck, ...] = ()
    score: float = 0.0
    baseline: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", str(self.candidate_id or "").strip())
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(
            self, "baseline", MappingProxyType(dict(self.baseline)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def from_checks(
        cls,
        candidate_id: str,
        checks: Tuple[EvaluationCheck, ...] | list[EvaluationCheck],
        *,
        baseline: Optional[Mapping[str, object]] = None,
        metadata: Optional[Mapping[str, object]] = None,
    ) -> "EvaluationReport":
        normalized = tuple(checks)
        required = [check for check in normalized if check.required]
        score = (
            sum(check.score for check in normalized) / len(normalized)
            if normalized else 0.0
        )
        return cls(
            candidate_id=candidate_id,
            passed=all(check.passed for check in required),
            checks=normalized,
            score=score,
            baseline=baseline or {},
            metadata=metadata or {},
        )


@dataclass(frozen=True)
class RollbackManifest:
    candidate_id: str
    artifacts: Tuple[Mapping[str, object], ...] = ()
    snapshot: Mapping[str, object] = field(default_factory=dict)
    instructions: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", str(self.candidate_id or "").strip())
        object.__setattr__(
            self, "artifacts",
            tuple(MappingProxyType(dict(item)) for item in self.artifacts))
        object.__setattr__(
            self, "snapshot", MappingProxyType(dict(self.snapshot)))
        object.__setattr__(
            self, "instructions", str(self.instructions or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ChangeApproval:
    approved: bool
    actor: str = ""
    reason: str = ""
    scope: str = ""
    approved_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved", bool(self.approved))
        for name in ("actor", "reason", "scope"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class ChangeCandidate:
    change_type: ChangeType
    target: str
    before: Mapping[str, object]
    after: Mapping[str, object]
    risk: RiskLevel = RiskLevel.MEDIUM
    affected_artifacts: Tuple[Mapping[str, object], ...] = ()
    evidence: Tuple[ChangeEvidence, ...] = ()
    candidate_id: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        change_type = ChangeType(self.change_type)
        risk = RiskLevel(self.risk)
        target = str(self.target or "").strip()
        before = dict(self.before)
        after = dict(self.after)
        artifacts = tuple(
            MappingProxyType(dict(item)) for item in self.affected_artifacts)
        evidence = tuple(self.evidence)
        payload = {
            "change_type": change_type.value,
            "target": target,
            "before": before,
            "after": after,
            "risk": risk.value,
            "artifacts": [dict(item) for item in artifacts],
            "evidence": [
                {
                    "kind": item.kind,
                    "source": item.source,
                    "digest": item.digest,
                    "passed": item.passed,
                }
                for item in evidence
            ],
        }
        candidate_id = str(self.candidate_id or "").strip() or (
            f"change-{_hash(payload)[:16]}")
        object.__setattr__(self, "change_type", change_type)
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "before", MappingProxyType(before))
        object.__setattr__(self, "after", MappingProxyType(after))
        object.__setattr__(self, "affected_artifacts", artifacts)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def candidate_hash(self) -> str:
        return _hash({
            "candidate_id": self.candidate_id,
            "change_type": self.change_type.value,
            "target": self.target,
            "before": dict(self.before),
            "after": dict(self.after),
            "risk": self.risk.value,
        })


@dataclass(frozen=True)
class AdmissionPolicy:
    require_evaluation: bool = True
    require_approval_at: RiskLevel = RiskLevel.HIGH
    require_rollback_at: RiskLevel = RiskLevel.HIGH


@dataclass(frozen=True)
class AdmissionDecision:
    status: AdmissionStatus
    candidate_id: str
    reason: str = ""
    release_id: str = ""
    policy_version: str = ""
    evaluation: Optional[EvaluationReport] = None
    rollback: Optional[RollbackManifest] = None
    approval: Optional[ChangeApproval] = None
    governance_decision: Optional[GovernanceDecision] = None
    audit: Mapping[str, object] = field(default_factory=dict)
    decided_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AdmissionStatus(self.status))
        object.__setattr__(
            self, "candidate_id", str(self.candidate_id or "").strip())
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self, "release_id", str(self.release_id or "").strip())
        object.__setattr__(
            self, "policy_version", str(self.policy_version or "").strip())
        object.__setattr__(
            self, "audit", MappingProxyType(dict(self.audit)))

    @property
    def admitted(self) -> bool:
        return self.status is AdmissionStatus.ADMITTED


class AdmissionGatePort(Protocol):
    def admit(
        self,
        candidate: ChangeCandidate,
        *,
        evaluation: Optional[EvaluationReport] = None,
        approval: Optional[ChangeApproval] = None,
        rollback: Optional[RollbackManifest] = None,
        policy_version: str = "",
        audit: Optional[Mapping[str, object]] = None,
    ) -> AdmissionDecision: ...


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AdmissionDecision",
    "AdmissionGatePort",
    "AdmissionPolicy",
    "AdmissionStatus",
    "ChangeApproval",
    "ChangeCandidate",
    "ChangeEvidence",
    "ChangeType",
    "EvaluationCheck",
    "EvaluationReport",
    "RollbackManifest",
]
