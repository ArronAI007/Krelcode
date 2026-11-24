"""DTOs and content-light state transitions for Prompt release proposals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, cast

from krelcode.agent.prompt.prompt_candidate import PromptCandidate
from krelcode.agent.prompt.prompt_release import ModelProfileCalibration
from krelcode.agent.prompt.prompt_release_support import integer, mapping


PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION = 1
PROMPT_RELEASE_PROPOSAL_KIND = "prompt_release_proposal"
PROMPT_HUMAN_APPROVAL_KIND = "prompt_human_approval"

_AGENT_APPROVERS = frozenset({"agent", "assistant", "model", "system", "krelcode"})


class PromptPipelineError(ValueError):
    """Invalid proposal input or state transition."""


class PromptProposalState(str, Enum):
    """Terminal and pending states of a Prompt release proposal."""

    BLOCKED_AUTHORING = "blocked_authoring"
    BLOCKED_ISOLATION = "blocked_isolation"
    PENDING_OFFLINE = "pending_offline"
    BLOCKED_OFFLINE = "blocked_offline"
    PENDING_LIVE = "pending_live"
    BLOCKED_LIVE = "blocked_live"
    BLOCKED_PARITY = "blocked_parity"
    BLOCKED_CONFLICT = "blocked_conflict"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    STALE = "stale"


class PromptPipelineStageStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    NOT_CREATED = "not_created"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, limit: int = 1_000) -> str:
    result = str(value or "").strip()
    if len(result) > limit:
        raise PromptPipelineError(f"字段超过 {limit} 字符上限")
    return result


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PromptPipelineError("布尔字段只接受 true/false")
    return value


def _report_payload(report: object) -> Mapping[str, object]:
    as_dict = getattr(report, "as_dict", None)
    if callable(as_dict):
        try:
            payload = as_dict(include_content=False)
        except TypeError:
            payload = as_dict()
        if isinstance(payload, Mapping):
            return payload
    if isinstance(report, Mapping):
        return report
    return {
        "type": type(report).__name__,
        "status": "available",
    }


def _report_hash(report: object) -> str:
    return _hash(_report_payload(report))


def _passed(report: object) -> bool:
    return bool(getattr(report, "passed", False))


def _manifest_hash(value: object) -> str:
    if value is None:
        return ""
    return _hash(_report_payload(value))


@dataclass(frozen=True)
class PromptPipelineRequest:
    """Input for one content-light release proposal."""

    candidate: PromptCandidate | Mapping[str, object]
    skill_name: str = "prompt-authoring"
    requested_by: str = "agent"
    profile_name: str = ""
    offline_report: object | None = None
    live_report: object | None = None
    parity_baseline: object | None = None
    run_offline: bool = True
    run_live: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate",
            self.candidate
            if isinstance(self.candidate, PromptCandidate)
            else PromptCandidate.from_mapping(self.candidate),
        )
        object.__setattr__(self, "skill_name", _text(self.skill_name, 160))
        object.__setattr__(self, "requested_by", _text(self.requested_by, 160))
        object.__setattr__(self, "profile_name", _text(self.profile_name, 160))
        object.__setattr__(self, "run_offline", bool(self.run_offline))
        object.__setattr__(self, "run_live", bool(self.run_live))

    @property
    def normalized_candidate(self) -> PromptCandidate:
        return cast(PromptCandidate, self.candidate)


@dataclass(frozen=True)
class PromptPipelineStage:
    id: str
    status: PromptPipelineStageStatus
    evidence_hash: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, 80))
        object.__setattr__(
            self, "status", PromptPipelineStageStatus(self.status))
        object.__setattr__(self, "evidence_hash", str(self.evidence_hash or ""))
        object.__setattr__(self, "details", dict(self.details))

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status.value,
            "evidence_hash": self.evidence_hash,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptPipelineStage":
        return cls(
            id=str(raw.get("id", "") or ""),
            status=PromptPipelineStageStatus(
                str(raw.get("status", "") or PromptPipelineStageStatus.PENDING.value)),
            evidence_hash=str(raw.get("evidence_hash", "") or ""),
            details=mapping(raw.get("details")),
        )


@dataclass(frozen=True)
class PromptReleaseProposal:
    """Immutable review proposal bound to content-light evidence identities."""

    schema_version: int
    proposal_id: str
    created_at: str
    candidate_hash: str
    candidate: Mapping[str, object]
    authoring_report_hash: str
    isolation_report_hash: str
    offline_report_hash: str
    live_report_hash: str
    parity_report_hash: str
    conflict_report_hash: str
    release_id: str
    release_hash: str
    profile_name: str
    profile_hash: str
    contract_version: str
    contract_hash: str
    suite_hash: str
    calibration: ModelProfileCalibration | None
    stages: tuple[PromptPipelineStage, ...]
    state: PromptProposalState
    requested_by: str
    skill_name: str
    approval_required: bool = True
    approved_by: str = ""
    approved_at: str = ""
    decision_reason: str = ""
    production_modified: bool = False
    applied: bool = False
    contains_prompt_content: bool = False
    kind: str = PROMPT_RELEASE_PROPOSAL_KIND

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate", dict(self.candidate))
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "state", PromptProposalState(self.state))
        if self.schema_version != PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION:
            raise PromptPipelineError(
                f"不支持的 Prompt release proposal schema: {self.schema_version}")
        if self.kind != PROMPT_RELEASE_PROPOSAL_KIND:
            raise PromptPipelineError("proposal kind 无效")
        if self.contains_prompt_content:
            raise PromptPipelineError("proposal 不得包含 Prompt 正文")
        if self.production_modified or self.applied:
            raise PromptPipelineError("proposal 不能在审批前修改生产 Prompt")
        if not self.candidate_hash or not self.proposal_id:
            raise PromptPipelineError("proposal 缺少 candidate/proposal identity")
        if self.approval_required is not True:
            raise PromptPipelineError("Prompt release 必须要求人工审批")

    @property
    def proposal_hash(self) -> str:
        """Stable evidence binding that survives approval-state updates."""

        return _hash({
            "schema_version": self.schema_version,
            "kind": self.kind,
            "candidate_hash": self.candidate_hash,
            "candidate": self.candidate,
            "authoring_report_hash": self.authoring_report_hash,
            "isolation_report_hash": self.isolation_report_hash,
            "offline_report_hash": self.offline_report_hash,
            "live_report_hash": self.live_report_hash,
            "parity_report_hash": self.parity_report_hash,
            "conflict_report_hash": self.conflict_report_hash,
            "release_id": self.release_id,
            "release_hash": self.release_hash,
            "profile_name": self.profile_name,
            "profile_hash": self.profile_hash,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "suite_hash": self.suite_hash,
            "calibration_hash": (
                self.calibration.calibration_hash
                if self.calibration is not None else ""),
        })

    @property
    def approval_pending(self) -> bool:
        return self.state is PromptProposalState.PENDING_APPROVAL

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "created_at": self.created_at,
            "candidate_hash": self.candidate_hash,
            "candidate": dict(self.candidate),
            "authoring_report_hash": self.authoring_report_hash,
            "isolation_report_hash": self.isolation_report_hash,
            "offline_report_hash": self.offline_report_hash,
            "live_report_hash": self.live_report_hash,
            "parity_report_hash": self.parity_report_hash,
            "conflict_report_hash": self.conflict_report_hash,
            "release_id": self.release_id,
            "release_hash": self.release_hash,
            "profile_name": self.profile_name,
            "profile_hash": self.profile_hash,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "suite_hash": self.suite_hash,
            "calibration": (
                self.calibration.as_dict()
                if self.calibration is not None else None),
            "stages": [stage.as_dict() for stage in self.stages],
            "state": self.state.value,
            "requested_by": self.requested_by,
            "skill_name": self.skill_name,
            "approval_required": self.approval_required,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "decision_reason": self.decision_reason,
            "production_modified": self.production_modified,
            "applied": self.applied,
            "contains_prompt_content": self.contains_prompt_content,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptReleaseProposal":
        if not isinstance(raw, Mapping):
            raise PromptPipelineError("proposal 根节点必须是 object")
        calibration_raw = raw.get("calibration")
        calibration = (
            ModelProfileCalibration.from_dict(calibration_raw)
            if isinstance(calibration_raw, Mapping) else None)
        stages_raw = raw.get("stages", ())
        if not isinstance(stages_raw, (list, tuple)):
            raise PromptPipelineError("proposal stages 必须是数组")
        candidate = raw.get("candidate")
        if not isinstance(candidate, Mapping):
            raise PromptPipelineError("proposal candidate 必须是 object")
        proposal = cls(
            schema_version=integer(raw.get("schema_version")),
            proposal_id=str(raw.get("proposal_id", "") or ""),
            created_at=str(raw.get("created_at", "") or ""),
            candidate_hash=str(raw.get("candidate_hash", "") or ""),
            candidate=dict(candidate),
            authoring_report_hash=str(raw.get("authoring_report_hash", "") or ""),
            isolation_report_hash=str(raw.get("isolation_report_hash", "") or ""),
            offline_report_hash=str(raw.get("offline_report_hash", "") or ""),
            live_report_hash=str(raw.get("live_report_hash", "") or ""),
            parity_report_hash=str(raw.get("parity_report_hash", "") or ""),
            conflict_report_hash=str(raw.get("conflict_report_hash", "") or ""),
            release_id=str(raw.get("release_id", "") or ""),
            release_hash=str(raw.get("release_hash", "") or ""),
            profile_name=str(raw.get("profile_name", "") or ""),
            profile_hash=str(raw.get("profile_hash", "") or ""),
            contract_version=str(raw.get("contract_version", "") or ""),
            contract_hash=str(raw.get("contract_hash", "") or ""),
            suite_hash=str(raw.get("suite_hash", "") or ""),
            calibration=calibration,
            stages=tuple(
                PromptPipelineStage.from_dict(item)
                for item in stages_raw if isinstance(item, Mapping)),
            state=PromptProposalState(
                str(raw.get("state", "") or PromptProposalState.STALE.value)),
            requested_by=str(raw.get("requested_by", "") or ""),
            skill_name=str(raw.get("skill_name", "") or ""),
            approval_required=bool(raw.get("approval_required", True)),
            approved_by=str(raw.get("approved_by", "") or ""),
            approved_at=str(raw.get("approved_at", "") or ""),
            decision_reason=str(raw.get("decision_reason", "") or ""),
            production_modified=bool(raw.get("production_modified", False)),
            applied=bool(raw.get("applied", False)),
            contains_prompt_content=bool(
                raw.get("contains_prompt_content", False)),
            kind=str(raw.get("kind", PROMPT_RELEASE_PROPOSAL_KIND) or ""),
        )
        stored_hash = str(raw.get("proposal_hash", "") or "")
        if stored_hash and stored_hash != proposal.proposal_hash:
            raise PromptPipelineError("proposal_hash 与 proposal 证据不一致")
        return proposal


@dataclass(frozen=True)
class PromptHumanApproval:
    schema_version: int
    kind: str
    proposal_id: str
    proposal_hash: str
    candidate_hash: str
    decision: str
    approved_by: str
    approved_at: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION:
            raise PromptPipelineError("不支持的 approval schema")
        if self.kind != PROMPT_HUMAN_APPROVAL_KIND:
            raise PromptPipelineError("approval kind 无效")
        if not self.proposal_id or not self.proposal_hash or not self.candidate_hash:
            raise PromptPipelineError("approval 缺少 proposal/candidate identity")
        if self.decision not in {"approved", "rejected"}:
            raise PromptPipelineError("approval decision 必须是 approved/rejected")
        actor = self.approved_by.strip()
        if not actor or actor.lower() in _AGENT_APPROVERS:
            raise PromptPipelineError("Prompt release 只能由人类显式批准或拒绝")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "candidate_hash": self.candidate_hash,
            "decision": self.decision,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "reason": self.reason,
            "production_modified": False,
            "applied": False,
            "contains_prompt_content": False,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptHumanApproval":
        return cls(
            schema_version=integer(raw.get("schema_version")),
            kind=str(raw.get("kind", "") or ""),
            proposal_id=str(raw.get("proposal_id", "") or ""),
            proposal_hash=str(raw.get("proposal_hash", "") or ""),
            candidate_hash=str(raw.get("candidate_hash", "") or ""),
            decision=str(raw.get("decision", "") or ""),
            approved_by=str(raw.get("approved_by", "") or ""),
            approved_at=str(raw.get("approved_at", "") or ""),
            reason=str(raw.get("reason", "") or ""),
        )



__all__ = [
    "PROMPT_HUMAN_APPROVAL_KIND",
    "PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION",
    "PROMPT_RELEASE_PROPOSAL_KIND",
    "PromptHumanApproval",
    "PromptPipelineError",
    "PromptPipelineRequest",
    "PromptPipelineStage",
    "PromptPipelineStageStatus",
    "PromptProposalState",
    "PromptReleaseProposal",
]
