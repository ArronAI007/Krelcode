"""Adapt Prompt release proposals to the universal change pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from ...contracts.application import RiskLevel
from ...contracts.change_admission import (
    ChangeCandidate,
    ChangeEvidence,
    ChangeType,
    EvaluationCheck,
    EvaluationReport,
    RollbackManifest,
)
from .prompt_release_models import (
    PromptPipelineStageStatus,
    PromptReleaseProposal,
)


@dataclass(frozen=True)
class PromptChangeAdmissionInputs:
    candidate: ChangeCandidate
    evaluation: EvaluationReport
    rollback: RollbackManifest


def prompt_change_inputs(
    proposal: PromptReleaseProposal,
) -> PromptChangeAdmissionInputs:
    candidate = ChangeCandidate(
        change_type=ChangeType.PROMPT,
        target=proposal.profile_name or proposal.release_id,
        before={
            "release_id": proposal.release_id,
            "release_hash": proposal.release_hash,
            "profile_hash": proposal.profile_hash,
        },
        after={
            "proposal_id": proposal.proposal_id,
            "candidate_hash": proposal.candidate_hash,
            "release_id": proposal.release_id,
            "release_hash": proposal.release_hash,
        },
        risk=RiskLevel.HIGH,
        affected_artifacts=(
            {"kind": "prompt_release", "id": proposal.release_id},
            {"kind": "model_profile", "id": proposal.profile_name},
        ),
        evidence=(
            ChangeEvidence(
                kind="offline_benchmark",
                source="prompt-release-pipeline",
                digest=proposal.offline_report_hash,
                passed=True,
            ),
            ChangeEvidence(
                kind="live_benchmark",
                source="prompt-release-pipeline",
                digest=proposal.live_report_hash,
                passed=True,
            ),
            ChangeEvidence(
                kind="parity_gate",
                source="prompt-release-pipeline",
                digest=proposal.parity_report_hash,
                passed=True,
            ),
        ),
    )
    checks = [
        EvaluationCheck(
            name=f"prompt_stage:{stage.id}",
            passed=stage.status is PromptPipelineStageStatus.PASSED,
            reason=stage.status.value,
            required=stage.status in {
                PromptPipelineStageStatus.PASSED,
                PromptPipelineStageStatus.FAILED,
            },
            score=1.0 if stage.status is PromptPipelineStageStatus.PASSED else 0.0,
        )
        for stage in proposal.stages
    ]
    evaluation = EvaluationReport.from_checks(
        candidate.candidate_id,
        checks,
        metadata={"proposal_hash": proposal.proposal_hash},
    )
    rollback = RollbackManifest(
        candidate_id=candidate.candidate_id,
        snapshot={
            "release_id": proposal.release_id,
            "release_hash": proposal.release_hash,
            "profile_hash": proposal.profile_hash,
        },
        instructions="恢复审批前的 Prompt release 与 model profile 哈希",
    )
    return PromptChangeAdmissionInputs(
        candidate=candidate,
        evaluation=evaluation,
        rollback=rollback,
    )


__all__ = ["PromptChangeAdmissionInputs", "prompt_change_inputs"]
