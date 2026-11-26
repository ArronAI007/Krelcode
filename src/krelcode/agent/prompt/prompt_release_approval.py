"""Human-only approval transitions for Prompt release proposals."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Optional, Sequence

from krelcode.agent.prompt.prompt_parity import (
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
)
from krelcode.agent.prompt.prompt_release import PromptRelease
from krelcode.agent.prompt.prompt_release_models import (
    PROMPT_HUMAN_APPROVAL_KIND,
    PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION,
    PromptHumanApproval,
    PromptPipelineError,
    PromptPipelineStage,
    PromptPipelineStageStatus,
    PromptProposalState,
    PromptReleaseProposal,
    _hash,
    _now,
    _text,
)
from krelcode.agent.prompt.prompt_release_store import PromptReleaseProposalStore
from krelcode.agent.prompts import CURRENT_PROMPT_RELEASE
from krelcode.contracts.change_admission import (
    AdmissionGatePort,
    ChangeApproval,
)
from krelcode.safety.admission import DefaultAdmissionGate
from .prompt_change_adapter import prompt_change_inputs


class PromptReleaseApprovalService:
    """Human-only proposal decisions and local approval artifacts."""

    def __init__(
        self,
        store: PromptReleaseProposalStore,
        *,
        release: PromptRelease = CURRENT_PROMPT_RELEASE,
        clock: Callable[[], str] = _now,
        admission_gate: Optional[AdmissionGatePort] = None,
    ) -> None:
        self.store = store
        self.release = release
        self.clock = clock
        self.admission_gate = admission_gate or DefaultAdmissionGate()

    def get(self, proposal_id: str) -> PromptReleaseProposal:
        return self.store.load_proposal(proposal_id)

    def list(self, limit: int = 50) -> tuple[PromptReleaseProposal, ...]:
        return self.store.list_proposals(limit)

    def approve(
        self,
        proposal_id: str,
        candidate_hash: str,
        *,
        approved_by: str,
        reason: str = "",
    ) -> PromptReleaseProposal:
        proposal = self.store.load_proposal(proposal_id)
        if proposal.candidate_hash != str(candidate_hash or "").strip():
            raise PromptPipelineError("proposal_id 与 candidate_hash 不匹配")
        if proposal.state is PromptProposalState.STALE:
            raise PromptPipelineError("当前 proposal 已 stale，必须重新评估")
        if proposal.state is not PromptProposalState.PENDING_APPROVAL:
            raise PromptPipelineError(
                f"proposal 当前状态不可审批: {proposal.state.value}")
        if not self._identity_current(proposal):
            stale = replace(
                proposal,
                state=PromptProposalState.STALE,
                decision_reason="release/profile/contract/suite identity changed",
            )
            self.store.save_proposal(stale)
            raise PromptPipelineError("proposal 身份已漂移，已标记为 stale")
        actor = _text(approved_by, 160)
        if actor.lower() in {
            "agent", "assistant", "model", "ai", "bot", "claude", "codex",
        }:
            raise PromptPipelineError("Prompt release 只能由人类显式批准或拒绝")
        inputs = prompt_change_inputs(proposal)
        admission = self.admission_gate.admit(
            inputs.candidate,
            evaluation=inputs.evaluation,
            approval=ChangeApproval(
                approved=True,
                actor=actor,
                reason=reason or "用户批准 Prompt release",
                scope="prompt-release",
                metadata={"proposal_id": proposal_id},
            ),
            rollback=inputs.rollback,
            policy_version=self.release.release_id,
            audit={"proposal_id": proposal_id},
        )
        if not admission.admitted:
            raise PromptPipelineError(
                "Prompt release 未通过运行时准入: "
                f"{admission.status.value}: {admission.reason}")
        return self._decide(
            proposal_id,
            candidate_hash,
            decision="approved",
            approved_by=actor,
            reason=reason,
        )

    def reject(
        self,
        proposal_id: str,
        candidate_hash: str,
        *,
        approved_by: str,
        reason: str,
    ) -> PromptReleaseProposal:
        if not str(reason).strip():
            raise PromptPipelineError("拒绝 Prompt release 必须填写原因")
        return self._decide(
            proposal_id,
            candidate_hash,
            decision="rejected",
            approved_by=approved_by,
            reason=reason,
        )

    def _decide(
        self,
        proposal_id: str,
        candidate_hash: str,
        *,
        decision: str,
        approved_by: str,
        reason: str,
    ) -> PromptReleaseProposal:
        proposal = self.store.load_proposal(proposal_id)
        if proposal.candidate_hash != str(candidate_hash or "").strip():
            raise PromptPipelineError("proposal_id 与 candidate_hash 不匹配")
        if proposal.state is PromptProposalState.STALE:
            raise PromptPipelineError("当前 proposal 已 stale，必须重新评估")
        if proposal.state is not PromptProposalState.PENDING_APPROVAL:
            raise PromptPipelineError(
                f"proposal 当前状态不可审批: {proposal.state.value}")
        if not self._identity_current(proposal):
            stale = replace(
                proposal,
                state=PromptProposalState.STALE,
                decision_reason="release/profile/contract/suite identity changed",
            )
            self.store.save_proposal(stale)
            raise PromptPipelineError("proposal 身份已漂移，已标记为 stale")
        actor = _text(approved_by, 160)
        timestamp = self.clock()
        approval = PromptHumanApproval(
            schema_version=PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION,
            kind=PROMPT_HUMAN_APPROVAL_KIND,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.proposal_hash,
            candidate_hash=proposal.candidate_hash,
            decision=decision,
            approved_by=actor,
            approved_at=timestamp,
            reason=_text(reason, 4_000),
        )
        self.store.save_approval(approval)
        artifacts: list[str] = []
        if decision == "approved" and proposal.calibration is not None:
            calibration_path = self.store.save_calibration(
                proposal.proposal_id, proposal.calibration)
            artifacts.append(calibration_path.name)
        decision_stage = PromptPipelineStage(
            "human_approval",
            (
                PromptPipelineStageStatus.PASSED
                if decision == "approved"
                else PromptPipelineStageStatus.FAILED
            ),
            _hash(approval.as_dict()),
            {
                "approved_by": approval.approved_by,
                "approved_at": approval.approved_at,
                "decision": approval.decision,
                "agent_may_approve": False,
                "contains_prompt_content": False,
            },
        )
        stages = self._replace_stage(proposal.stages, decision_stage)
        if decision == "approved":
            artifact_stage = PromptPipelineStage(
                "release_artifacts",
                PromptPipelineStageStatus.PASSED,
                details={
                    "artifacts": artifacts,
                    "production_modified": False,
                    "applied": False,
                    "contains_prompt_content": False,
                },
            )
            stages = self._replace_stage(stages, artifact_stage)
        updated = replace(
            proposal,
            stages=stages,
            state=(
                PromptProposalState.APPROVED
                if decision == "approved" else PromptProposalState.REJECTED
            ),
            approved_by=approval.approved_by,
            approved_at=approval.approved_at,
            decision_reason=approval.reason,
        )
        if decision == "approved":
            artifact_path = self.store.save_release_artifact(updated, approval)
            stages = self._replace_stage(
                updated.stages,
                PromptPipelineStage(
                    "release_artifacts",
                    PromptPipelineStageStatus.PASSED,
                    details={
                        "artifacts": [*artifacts, artifact_path.name],
                        "production_modified": False,
                        "applied": False,
                        "contains_prompt_content": False,
                    },
                ),
            )
            updated = replace(updated, stages=stages)
        self.store.save_proposal(updated)
        return updated

    def _identity_current(self, proposal: PromptReleaseProposal) -> bool:
        if proposal.release_id != self.release.release_id:
            return False
        if proposal.release_hash != self.release.release_hash:
            return False
        if proposal.contract_version != PROMPT_PARITY_CONTRACT.version:
            return False
        if proposal.contract_hash != PROMPT_PARITY_CONTRACT.hash:
            return False
        if (
            proposal.suite_hash
            and proposal.suite_hash != FROZEN_PROMPT_PARITY_SUITE_HASH
        ):
            return False
        if proposal.profile_name:
            try:
                profile = self.release.profile(proposal.profile_name)
            except ValueError:
                return False
            if proposal.profile_hash != profile.profile_hash:
                return False
        return True

    @staticmethod
    def _replace_stage(
        stages: Sequence[PromptPipelineStage],
        replacement: PromptPipelineStage,
    ) -> tuple[PromptPipelineStage, ...]:
        found = False
        result = []
        for stage in stages:
            if stage.id == replacement.id:
                result.append(replacement)
                found = True
            else:
                result.append(stage)
        if not found:
            result.append(replacement)
        return tuple(result)



__all__ = ["PromptReleaseApprovalService"]
