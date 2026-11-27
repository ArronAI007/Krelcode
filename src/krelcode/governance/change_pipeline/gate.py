"""Universal runtime admission gate for hot changes."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Mapping, Optional

from ...contracts.application import RiskLevel
from ...contracts.change_admission import (
    AdmissionDecision,
    AdmissionPolicy,
    AdmissionStatus,
    ChangeApproval,
    ChangeCandidate,
    ChangeType,
    EvaluationReport,
    RollbackManifest,
)
from ...contracts.governance import (
    GovernanceDecision,
)


_RISK_RANK = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

DEFAULT_ADMISSION_POLICIES: Mapping[ChangeType, AdmissionPolicy] = {
    ChangeType.PROMPT: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.HIGH,
        require_rollback_at=RiskLevel.HIGH,
    ),
    ChangeType.PLUGIN: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.MEDIUM,
        require_rollback_at=RiskLevel.MEDIUM,
    ),
    ChangeType.SKILL: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.HIGH,
        require_rollback_at=RiskLevel.MEDIUM,
    ),
    ChangeType.AGENT_DEFINITION: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.MEDIUM,
        require_rollback_at=RiskLevel.MEDIUM,
    ),
    ChangeType.MODEL_PROFILE: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.HIGH,
        require_rollback_at=RiskLevel.MEDIUM,
    ),
    ChangeType.TOOL_CATALOG: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.HIGH,
        require_rollback_at=RiskLevel.HIGH,
    ),
    ChangeType.MCP_SERVER: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.HIGH,
        require_rollback_at=RiskLevel.HIGH,
    ),
    ChangeType.MEMORY_MIGRATION: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.MEDIUM,
        require_rollback_at=RiskLevel.MEDIUM,
    ),
    ChangeType.AUTO_DREAM: AdmissionPolicy(
        require_evaluation=True,
        require_approval_at=RiskLevel.MEDIUM,
        require_rollback_at=RiskLevel.MEDIUM,
    ),
}


class DefaultAdmissionGate:
    """Apply one admission lifecycle with per-change-type policies."""

    def __init__(
        self,
        policies: Optional[Mapping[ChangeType, AdmissionPolicy]] = None,
    ) -> None:
        self.policies = {
            **DEFAULT_ADMISSION_POLICIES,
            **dict(policies or {}),
        }

    def admit(
        self,
        candidate: ChangeCandidate,
        *,
        evaluation: Optional[EvaluationReport] = None,
        approval: Optional[ChangeApproval] = None,
        rollback: Optional[RollbackManifest] = None,
        policy_version: str = "",
        audit: Optional[Mapping[str, object]] = None,
        governance_decision: GovernanceDecision,
    ) -> AdmissionDecision:
        policy = self.policies[candidate.change_type]
        governance = governance_decision

        def decide(
            status: AdmissionStatus,
            item: ChangeCandidate,
            reason: str,
            *,
            policy_version: str,
            evaluation: Optional[EvaluationReport],
            rollback: Optional[RollbackManifest],
            approval: Optional[ChangeApproval],
            audit: Optional[Mapping[str, object]],
        ) -> AdmissionDecision:
            return self._decision(
                status,
                item,
                reason,
                governance_decision=governance,
                policy_version=policy_version,
                evaluation=evaluation,
                rollback=rollback,
                approval=approval,
                audit=audit,
            )
        failed_evidence = [
            item for item in candidate.evidence if not item.passed]
        if failed_evidence:
            return decide(
                AdmissionStatus.REJECTED,
                candidate,
                "候选变更包含失败证据: " + ", ".join(
                    item.kind or item.source for item in failed_evidence),
                policy_version=policy_version,
                evaluation=evaluation,
                rollback=rollback,
                approval=approval,
                audit=audit,
            )
        if candidate.expires_at > 0 and candidate.expires_at <= time.time():
            return decide(
                AdmissionStatus.REJECTED,
                candidate,
                "候选变更已过期",
                policy_version=policy_version,
                evaluation=evaluation,
                rollback=rollback,
                approval=approval,
                audit=audit,
            )
        if policy.require_evaluation:
            if evaluation is None:
                return decide(
                    AdmissionStatus.REJECTED,
                    candidate,
                    "缺少 EvaluationReport",
                    policy_version=policy_version,
                    evaluation=evaluation,
                    rollback=rollback,
                    approval=approval,
                    audit=audit,
                )
            if evaluation.candidate_id != candidate.candidate_id:
                return decide(
                    AdmissionStatus.REJECTED,
                    candidate,
                    "EvaluationReport 与候选变更不匹配",
                    policy_version=policy_version,
                    evaluation=evaluation,
                    rollback=rollback,
                    approval=approval,
                    audit=audit,
                )
            if not evaluation.passed:
                return decide(
                    AdmissionStatus.REJECTED,
                    candidate,
                    "评估未通过",
                    policy_version=policy_version,
                    evaluation=evaluation,
                    rollback=rollback,
                    approval=approval,
                    audit=audit,
                )
        if _risk_at_least(candidate.risk, policy.require_rollback_at):
            if rollback is None:
                return decide(
                    AdmissionStatus.REJECTED,
                    candidate,
                    "高风险变更缺少 RollbackManifest",
                    policy_version=policy_version,
                    evaluation=evaluation,
                    rollback=rollback,
                    approval=approval,
                    audit=audit,
                )
            if rollback.candidate_id != candidate.candidate_id:
                return decide(
                    AdmissionStatus.REJECTED,
                    candidate,
                    "RollbackManifest 与候选变更不匹配",
                    policy_version=policy_version,
                    evaluation=evaluation,
                    rollback=rollback,
                    approval=approval,
                    audit=audit,
                )
        if _risk_at_least(candidate.risk, policy.require_approval_at):
            if approval is None:
                return decide(
                    AdmissionStatus.PENDING_APPROVAL,
                    candidate,
                    "变更需要人工审批",
                    policy_version=policy_version,
                    evaluation=evaluation,
                    rollback=rollback,
                    approval=approval,
                    audit=audit,
                )
            if not approval.approved:
                return decide(
                    AdmissionStatus.REJECTED,
                    candidate,
                    approval.reason or "审批被拒绝",
                    policy_version=policy_version,
                    evaluation=evaluation,
                    rollback=rollback,
                    approval=approval,
                    audit=audit,
                )
        return decide(
            AdmissionStatus.ADMITTED,
            candidate,
            "",
            policy_version=policy_version,
            evaluation=evaluation,
            rollback=rollback,
            approval=approval,
            audit=audit,
        )

    def _decision(
        self,
        status: AdmissionStatus,
        candidate: ChangeCandidate,
        reason: str,
        *,
        policy_version: str,
        evaluation: Optional[EvaluationReport],
        rollback: Optional[RollbackManifest],
        approval: Optional[ChangeApproval],
        audit: Optional[Mapping[str, object]],
        governance_decision: GovernanceDecision,
    ) -> AdmissionDecision:
        release_id = ""
        if status is AdmissionStatus.ADMITTED:
            release_id = "admission-" + _hash({
                "candidate": candidate.candidate_hash,
                "evaluation": (
                    {
                        "passed": evaluation.passed,
                        "score": evaluation.score,
                    }
                    if evaluation is not None else None
                ),
                "approval": (
                    {
                        "actor": approval.actor,
                        "scope": approval.scope,
                    }
                    if approval is not None else None
                ),
                "rollback": (
                    dict(rollback.snapshot)
                    if rollback is not None else None
                ),
                "policy_version": policy_version,
            })[:20]
        return AdmissionDecision(
            status=status,
            candidate_id=candidate.candidate_id,
            reason=reason,
            release_id=release_id,
            policy_version=policy_version,
            evaluation=evaluation,
            rollback=rollback,
            approval=approval,
            governance_decision=governance_decision,
            security_decision=governance_decision.security_decision,
            audit={
                "change_type": candidate.change_type.value,
                "target": candidate.target,
                "risk": candidate.risk.value,
                **dict(audit or {}),
            },
        )


AdmissionGate = DefaultAdmissionGate


def _risk_at_least(value: RiskLevel, minimum: RiskLevel) -> bool:
    return _RISK_RANK[value] >= _RISK_RANK[minimum]


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AdmissionGate",
    "DEFAULT_ADMISSION_POLICIES",
    "DefaultAdmissionGate",
]
