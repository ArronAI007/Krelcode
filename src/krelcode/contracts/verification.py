"""Risk-aware verification levels and external verifier contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, Tuple

from .acceptance import AcceptanceCheck, AcceptanceEvidence, AcceptanceStatus
from .application import ApplicationRequest, RiskLevel


class VerificationLevel(str, Enum):
    STRUCTURAL = "structural"
    ENVIRONMENT = "environment"
    DETERMINISTIC = "deterministic"
    INDEPENDENT_MODEL = "independent_model"
    HUMAN = "human"


@dataclass(frozen=True)
class VerificationPolicy:
    minimum_level: VerificationLevel | str
    required_levels: Tuple[VerificationLevel, ...] = ()
    require_independent_verifier: bool = False
    require_human_approval: bool = False
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "minimum_level", VerificationLevel(self.minimum_level))
        object.__setattr__(
            self,
            "required_levels",
            tuple(dict.fromkeys(
                VerificationLevel(item) for item in self.required_levels
            )),
        )
        object.__setattr__(
            self,
            "require_independent_verifier",
            bool(self.require_independent_verifier),
        )
        object.__setattr__(
            self,
            "require_human_approval",
            bool(self.require_human_approval),
        )
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def for_request(
        cls,
        request: ApplicationRequest,
    ) -> "VerificationPolicy":
        risk = RiskLevel(request.risk.level)
        levels: list[VerificationLevel] = [VerificationLevel.STRUCTURAL]
        reason = f"risk={risk.value}"
        required_evidence = {
            item.casefold().replace("_", "-")
            for item in request.acceptance.required_evidence
        }
        if required_evidence.intersection({
            "tool-result", "test", "tests", "test-result",
        }):
            levels.append(VerificationLevel.ENVIRONMENT)
        if required_evidence.intersection({
            "test", "tests", "test-result",
        }):
            levels.append(VerificationLevel.DETERMINISTIC)
            reason += "; deterministic-test-evidence"
        independent = risk is RiskLevel.CRITICAL
        human = (
            risk is RiskLevel.CRITICAL
            or bool(request.risk.requires_approval)
        )
        if independent:
            levels.append(VerificationLevel.INDEPENDENT_MODEL)
        if human:
            levels.append(VerificationLevel.HUMAN)
        return cls(
            minimum_level=(
                VerificationLevel.HUMAN
                if human
                else VerificationLevel.INDEPENDENT_MODEL
                if independent
                else VerificationLevel.DETERMINISTIC
                if VerificationLevel.DETERMINISTIC in levels
                else VerificationLevel.ENVIRONMENT
                if VerificationLevel.ENVIRONMENT in levels
                else VerificationLevel.STRUCTURAL
            ),
            required_levels=tuple(levels),
            require_independent_verifier=independent,
            require_human_approval=human,
            reason=reason,
            metadata={
                "trigger": request.trigger.value,
                "risk_level": risk.value,
                "required_evidence": list(
                    request.acceptance.required_evidence),
            },
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "minimum_level": VerificationLevel(self.minimum_level).value,
            "required_levels": [
                VerificationLevel(item).value
                for item in self.required_levels
            ],
            "require_independent_verifier": (
                self.require_independent_verifier),
            "require_human_approval": self.require_human_approval,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class VerificationRequest:
    turn_id: str
    level: VerificationLevel | str
    goal: str
    final_response: str
    checks: Tuple[AcceptanceCheck, ...] = ()
    evidence: Tuple[AcceptanceEvidence, ...] = ()
    policy: VerificationPolicy | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "turn_id", str(self.turn_id or "").strip())
        object.__setattr__(self, "level", VerificationLevel(self.level))
        object.__setattr__(self, "goal", str(self.goal or "").strip())
        object.__setattr__(
            self, "final_response", str(self.final_response or ""))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class VerificationResult:
    level: VerificationLevel | str
    verifier_id: str
    status: AcceptanceStatus | str
    reason: str = ""
    checks: Tuple[AcceptanceCheck, ...] = ()
    evidence: Tuple[AcceptanceEvidence, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", VerificationLevel(self.level))
        object.__setattr__(
            self, "verifier_id", str(self.verifier_id or "").strip())
        if not self.verifier_id:
            raise ValueError("VerificationResult.verifier_id 不能为空")
        object.__setattr__(
            self, "status", AcceptanceStatus(self.status))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


class VerifierPort(Protocol):
    async def verify(
        self,
        request: VerificationRequest,
    ) -> VerificationResult: ...


__all__ = [
    "VerificationLevel",
    "VerificationPolicy",
    "VerificationRequest",
    "VerificationResult",
    "VerifierPort",
]
