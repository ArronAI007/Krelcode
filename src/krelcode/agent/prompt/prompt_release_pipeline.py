"""End-to-end Candidate Prompt release proposal pipeline.

This module connects the existing Prompt authoring, isolated evaluation,
benchmark, conflict-analysis and release/calibration components.  It has one
deliberate asymmetry: Agents may create and inspect proposals, but only the
human-facing approval service may approve or reject one.  Creating a proposal
never modifies production Prompt sources, the active Fragment Registry, or a
runtime configuration file.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, cast

from krelcode.agent.prompt.prompt_parity import (
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
    PromptParityGateResult,
    evaluate_prompt_parity,
)
from krelcode.agent.prompt.fragments import PromptFragmentRegistry
from krelcode.agent.prompt.prompt_authoring import PromptAuthoringValidator
from krelcode.agent.prompt.prompt_candidate import PromptCandidate
from krelcode.agent.prompt.prompt_conflicts import PromptConflictAnalyzer
from krelcode.agent.prompt.prompt_isolation import CandidateIsolationReport, PromptCandidateIsolator
from krelcode.agent.prompt.prompt_release_support import case_summary, integer, mapping, mapping_items
from krelcode.agent.prompt.prompt_release import (
    MODEL_PROFILE_CALIBRATION_SCHEMA_VERSION,
    ModelProfileCalibration,
    PromptRelease,
)
from krelcode.agent.prompt.prompts import CURRENT_PROMPT_RELEASE


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


class PromptReleaseProposalStore:
    """Atomic, workspace-local, content-light proposal and approval store."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.root = (
            Path(workspace_root).expanduser().resolve()
            / ".krelcode" / "prompt-releases"
        )
        self.proposals_dir = self.root / "proposals"
        self.approvals_dir = self.root / "approvals"
        self.calibrations_dir = self.root / "calibrations"
        self.artifacts_dir = self.root / "artifacts"

    def _safe_id(self, proposal_id: str) -> str:
        value = _text(proposal_id, 240)
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise PromptPipelineError("非法 proposal_id")
        return value

    def _atomic_write(self, path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def save_proposal(self, proposal: PromptReleaseProposal) -> Path:
        path = self.proposals_dir / f"{self._safe_id(proposal.proposal_id)}.json"
        if path.exists():
            existing = self.load_proposal(proposal.proposal_id)
            if existing.proposal_hash != proposal.proposal_hash:
                raise PromptPipelineError("同一 proposal_id 的证据身份发生漂移")
        self._atomic_write(path, proposal.as_dict())
        return path

    def load_proposal(self, proposal_id: str) -> PromptReleaseProposal:
        path = self.proposals_dir / f"{self._safe_id(proposal_id)}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PromptPipelineError(f"无法读取 proposal: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PromptPipelineError(f"proposal JSON 无效: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise PromptPipelineError("proposal 根节点必须是 object")
        return PromptReleaseProposal.from_dict(raw)

    def list_proposals(self, limit: int = 50) -> tuple[PromptReleaseProposal, ...]:
        bounded = max(1, min(int(limit), 500))
        if not self.proposals_dir.is_dir():
            return ()
        proposals: list[PromptReleaseProposal] = []
        for path in self.proposals_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    proposals.append(PromptReleaseProposal.from_dict(raw))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        proposals.sort(
            key=lambda item: (item.created_at, item.proposal_id),
            reverse=True,
        )
        return tuple(proposals[:bounded])

    def save_approval(self, approval: PromptHumanApproval) -> Path:
        path = self.approvals_dir / f"{self._safe_id(approval.proposal_id)}.json"
        self._atomic_write(path, approval.as_dict())
        return path

    def load_approval(self, proposal_id: str) -> PromptHumanApproval:
        path = self.approvals_dir / f"{self._safe_id(proposal_id)}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PromptPipelineError(f"无法读取 approval: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise PromptPipelineError("approval 根节点必须是 object")
        return PromptHumanApproval.from_dict(raw)

    def save_calibration(
        self, proposal_id: str, calibration: ModelProfileCalibration,
    ) -> Path:
        path = (
            self.calibrations_dir
            / f"{self._safe_id(proposal_id)}.json"
        )
        self._atomic_write(path, calibration.as_dict())
        return path

    def save_release_artifact(
        self, proposal: PromptReleaseProposal, approval: PromptHumanApproval,
    ) -> Path:
        payload = {
            "schema_version": PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION,
            "kind": "approved_prompt_release_artifact",
            "proposal_id": proposal.proposal_id,
            "proposal_hash": proposal.proposal_hash,
            "candidate_hash": proposal.candidate_hash,
            "approval": approval.as_dict(),
            "release_id": proposal.release_id,
            "release_hash": proposal.release_hash,
            "profile_name": proposal.profile_name,
            "profile_hash": proposal.profile_hash,
            "calibration_hash": (
                proposal.calibration.calibration_hash
                if proposal.calibration is not None else ""),
            "production_modified": False,
            "applied": False,
            "contains_prompt_content": False,
        }
        path = self.artifacts_dir / f"{self._safe_id(proposal.proposal_id)}.json"
        self._atomic_write(path, payload)
        return path


class PromptReleasePipeline:
    """Connect Candidate, isolation, benchmarks, conflicts and calibration."""

    def __init__(
        self,
        registry_provider: Callable[[], Optional[PromptFragmentRegistry]],
        *,
        release: PromptRelease = CURRENT_PROMPT_RELEASE,
        conflict_analyzer: Optional[PromptConflictAnalyzer] = None,
        authoring_validator: Optional[PromptAuthoringValidator] = None,
        isolation_context: object | None = None,
        offline_runner: object | None = None,
        offline_runner_factory: Callable[[], object] | None = None,
        live_runner: object | None = None,
        parity_evaluator: Callable[..., PromptParityGateResult] = evaluate_prompt_parity,
        store: PromptReleaseProposalStore | None = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.registry_provider = registry_provider
        self.release = release
        self.conflict_analyzer = conflict_analyzer or PromptConflictAnalyzer()
        self.authoring_validator = (
            authoring_validator
            or PromptAuthoringValidator(self.conflict_analyzer)
        )
        self.isolation_context = isolation_context
        self.offline_runner = offline_runner
        self.offline_runner_factory = offline_runner_factory
        self.live_runner = live_runner
        self.parity_evaluator = parity_evaluator
        self.store = store
        self.clock = clock

    def prepare(self, request: PromptPipelineRequest) -> PromptReleaseProposal:
        import asyncio

        return asyncio.run(self.prepare_async(request))

    async def prepare_async(
        self, request: PromptPipelineRequest,
    ) -> PromptReleaseProposal:
        if not isinstance(request, PromptPipelineRequest):
            raise PromptPipelineError("request 必须是 PromptPipelineRequest")
        registry = self.registry_provider()
        if registry is None:
            raise PromptPipelineError("Prompt Fragment Registry 未配置")
        candidate = request.normalized_candidate
        target = registry.get(candidate.target_fragment)
        authoring_request = candidate.to_authoring_request(target)
        authoring = self.authoring_validator.validate(
            authoring_request,
            active_fragments=registry.list(include_disabled=False),
        )
        authoring_hash = authoring.report_hash

        stages: list[PromptPipelineStage] = [
            PromptPipelineStage(
                "authoring",
                (
                    PromptPipelineStageStatus.PASSED
                    if authoring.passed
                    else PromptPipelineStageStatus.FAILED
                ),
                authoring_hash,
                {
                    "contract_version": authoring.contract_version,
                    "error_count": authoring.error_count,
                    "warning_count": authoring.warning_count,
                    "contains_prompt_content": False,
                },
            ),
            PromptPipelineStage(
                "candidate",
                PromptPipelineStageStatus.PASSED,
                candidate.candidate_hash,
                {
                    "target_fragment": candidate.target_fragment,
                    "risk": candidate.risk_value,
                    "contains_prompt_content": False,
                },
            ),
        ]
        if not authoring.passed:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_AUTHORING,
                authoring_report_hash=authoring_hash,
            )

        isolation = PromptCandidateIsolator(
            self.registry_provider,
            context=self.isolation_context,
        ).evaluate(candidate, authoring_report=authoring)
        isolation_hash = _hash(isolation.as_dict())
        stages.append(PromptPipelineStage(
            "isolated_prompt_assembler",
            (
                PromptPipelineStageStatus.PASSED
                if isolation.review_allowed
                else PromptPipelineStageStatus.FAILED
            ),
            isolation_hash,
            {
                "mode": isolation.as_dict().get("mode", ""),
                "failed_checks": list(isolation.failed_checks),
                "stable_hash": isolation.candidate_plan.get("stable_hash", ""),
                "dynamic_hash": isolation.candidate_plan.get("dynamic_hash", ""),
                "full_hash": isolation.candidate_plan.get("full_hash", ""),
                "contains_prompt_content": False,
            },
        ))
        if not isolation.review_allowed:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_ISOLATION,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
            )

        offline_report = request.offline_report
        if offline_report is None and request.run_offline:
            runner = self.offline_runner
            if runner is None and self.offline_runner_factory is not None:
                runner = self.offline_runner_factory()
            if runner is None:
                raise PromptPipelineError(
                    "offline benchmark runner 未配置；请由入口层注入")
            offline_report = await self._invoke_runner(runner, "offline")
        if offline_report is None:
            stages.append(PromptPipelineStage(
                "offline_benchmark",
                PromptPipelineStageStatus.PENDING,
                details={"run_offline": False, "contains_prompt_content": False},
            ))
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.PENDING_OFFLINE,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
            )

        offline_hash = _report_hash(offline_report)
        offline_passed = _passed(offline_report)
        stages.append(PromptPipelineStage(
            "offline_benchmark",
            (
                PromptPipelineStageStatus.PASSED
                if offline_passed else PromptPipelineStageStatus.FAILED
            ),
            offline_hash,
            self._benchmark_summary(offline_report, kind="offline"),
        ))
        if not offline_passed:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_OFFLINE,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
                offline_report_hash=offline_hash,
            )

        conflict_report = isolation.conflict_report
        blocking_conflicts = tuple(
            item for item in mapping_items(conflict_report, "conflicts")
            if (
                candidate.target_fragment in {
                    item.get("left_fragment_id", ""),
                    item.get("right_fragment_id", ""),
                }
                and (
                    bool(item.get("unresolved", False))
                    or item.get("winner_fragment_id", "")
                    != candidate.target_fragment
                )
            )
        )
        conflict_hash = _hash(conflict_report)
        conflict_passed = not blocking_conflicts
        stages.append(PromptPipelineStage(
            "conflict_analysis",
            (
                PromptPipelineStageStatus.PASSED
                if conflict_passed else PromptPipelineStageStatus.FAILED
            ),
            conflict_hash,
            {
                "conflict_count": integer(conflict_report.get("conflict_count", 0)),
                "unresolved_count": integer(
                    conflict_report.get("unresolved_count", 0)),
                "blocking_conflict_ids": [
                    str(item.get("id", "")) for item in blocking_conflicts],
                "contains_prompt_content": False,
            },
        ))
        if not conflict_passed:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_CONFLICT,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
                offline_report_hash=offline_hash,
                conflict_report_hash=conflict_hash,
            )

        live_report = request.live_report
        if live_report is None and request.run_live:
            if self.live_runner is None:
                return self._finish(
                    request, candidate, stages,
                    state=PromptProposalState.PENDING_LIVE,
                    authoring_report_hash=authoring_hash,
                    isolation=isolation,
                    offline_report_hash=offline_hash,
                    conflict_report_hash=conflict_hash,
                    issue="live_runner_unavailable",
                )
            live_report = await self._invoke_runner(self.live_runner, "live")
        if live_report is None:
            stages.append(PromptPipelineStage(
                "live_benchmark",
                PromptPipelineStageStatus.PENDING,
                details={"run_live": False, "contains_prompt_content": False},
            ))
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.PENDING_LIVE,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
                offline_report_hash=offline_hash,
                conflict_report_hash=conflict_hash,
            )

        live_hash = _report_hash(live_report)
        live_passed = _passed(live_report)
        stages.append(PromptPipelineStage(
            "live_benchmark",
            (
                PromptPipelineStageStatus.PASSED
                if live_passed else PromptPipelineStageStatus.FAILED
            ),
            live_hash,
            self._benchmark_summary(live_report, kind="live"),
        ))
        if not live_passed:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_LIVE,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
                offline_report_hash=offline_hash,
                live_report_hash=live_hash,
                conflict_report_hash=conflict_hash,
            )

        parity_hash = ""
        if request.parity_baseline is not None:
            parity = self.parity_evaluator(
                live_report, request.parity_baseline, PROMPT_PARITY_CONTRACT)
            parity_hash = _manifest_hash(parity)
            stages.append(PromptPipelineStage(
                "prompt_parity",
                (
                    PromptPipelineStageStatus.PASSED
                    if parity.passed else PromptPipelineStageStatus.FAILED
                ),
                parity_hash,
                {
                    "status": getattr(parity.status, "value", str(parity.status)),
                    "failed_checks": [
                        item.id for item in parity.checks if not item.passed],
                    "contains_prompt_content": False,
                },
            ))
            if not parity.passed:
                return self._finish(
                    request, candidate, stages,
                    state=PromptProposalState.BLOCKED_PARITY,
                    authoring_report_hash=authoring_hash,
                    isolation=isolation,
                    offline_report_hash=offline_hash,
                    live_report_hash=live_hash,
                    parity_report_hash=parity_hash,
                    conflict_report_hash=conflict_hash,
                )
        else:
            stages.append(PromptPipelineStage(
                "prompt_parity",
                PromptPipelineStageStatus.PENDING,
                details={
                    "reason": "no accepted parity baseline supplied",
                    "contains_prompt_content": False,
                },
            ))

        identity = self._live_identity(request, live_report)
        if identity["error"]:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_LIVE,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
                offline_report_hash=offline_hash,
                live_report_hash=live_hash,
                parity_report_hash=parity_hash,
                conflict_report_hash=conflict_hash,
                profile_name=str(identity["profile_name"] or ""),
                profile_hash=str(identity["profile_hash"] or ""),
                suite_hash=str(identity["suite_hash"] or ""),
                issue=str(identity["error"]),
            )

        provider = str(getattr(live_report, "provider", "") or "").strip()
        model = str(getattr(live_report, "model", "") or "").strip()
        schema_version = int(getattr(live_report, "schema_version", 0) or 0)
        if not provider or not model or schema_version < 1:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_LIVE,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
                offline_report_hash=offline_hash,
                live_report_hash=live_hash,
                parity_report_hash=parity_hash,
                conflict_report_hash=conflict_hash,
                profile_name=str(identity["profile_name"] or ""),
                profile_hash=str(identity["profile_hash"] or ""),
                suite_hash=str(identity["suite_hash"] or ""),
                issue="live-report-missing-provider-model-schema",
            )
        try:
            calibration = ModelProfileCalibration.certify(
                profile_name=str(identity["profile_name"]),
                profile_hash=str(identity["profile_hash"]),
                release_id=self.release.release_id,
                release_hash=self.release.release_hash,
                contract_version=PROMPT_PARITY_CONTRACT.version,
                contract_hash=PROMPT_PARITY_CONTRACT.hash,
                benchmark_schema_version=schema_version,
                suite_hash=str(identity["suite_hash"]),
                provider=provider,
                model=model,
                verified_at=self.clock(),
                trials=max(
                    1,
                    max((
                        integer(value.get("trials", 0))
                        for value in case_summary(live_report).values()
                    ), default=1),
                ),
            )
        except ValueError as exc:
            return self._finish(
                request, candidate, stages,
                state=PromptProposalState.BLOCKED_LIVE,
                authoring_report_hash=authoring_hash,
                isolation=isolation,
                offline_report_hash=offline_hash,
                live_report_hash=live_hash,
                parity_report_hash=parity_hash,
                conflict_report_hash=conflict_hash,
                profile_name=str(identity["profile_name"] or ""),
                profile_hash=str(identity["profile_hash"] or ""),
                suite_hash=str(identity["suite_hash"] or ""),
                issue=f"calibration-failed:{type(exc).__name__}",
            )
        stages.append(PromptPipelineStage(
            "release_calibration",
            PromptPipelineStageStatus.PASSED,
            calibration.calibration_hash,
            {
                "calibration_id": calibration.calibration_id,
                "profile_name": calibration.profile_name,
                "provider": calibration.provider,
                "model": calibration.model,
                "trials": calibration.trials,
                "contains_prompt_content": False,
            },
        ))
        stages.append(PromptPipelineStage(
            "human_approval",
            PromptPipelineStageStatus.PENDING,
            details={
                "approval_required": True,
                "agent_may_approve": False,
                "contains_prompt_content": False,
            },
        ))
        stages.append(PromptPipelineStage(
            "release_artifacts",
            PromptPipelineStageStatus.NOT_CREATED,
            details={"production_modified": False, "applied": False},
        ))
        return self._finish(
            request, candidate, stages,
            state=PromptProposalState.PENDING_APPROVAL,
            authoring_report_hash=authoring_hash,
            isolation=isolation,
            offline_report_hash=offline_hash,
            live_report_hash=live_hash,
            parity_report_hash=parity_hash,
            conflict_report_hash=conflict_hash,
            profile_name=str(identity["profile_name"]),
            profile_hash=str(identity["profile_hash"]),
            suite_hash=str(identity["suite_hash"]),
            calibration=calibration,
        )

    async def _invoke_runner(self, runner: object, label: str) -> object:
        method = getattr(runner, "run_async", None)
        if not callable(method):
            method = getattr(runner, "run", None)
        if not callable(method):
            raise PromptPipelineError(f"{label} benchmark runner 不可调用")
        result = method()
        if inspect.isawaitable(result):
            result = await result
        return result

    def _benchmark_summary(
        self, report: object, *, kind: str,
    ) -> dict[str, object]:
        summary = case_summary(report)
        errors = getattr(report, "error_summary", None)
        error_summary = errors() if callable(errors) else {}
        cases = len(summary)
        trials = sum(
            integer(item.get("trials", 0))
            for item in summary.values()
        )
        return {
            "kind": kind,
            "passed": _passed(report),
            "schema_version": int(getattr(report, "schema_version", 0) or 0),
            "provider": str(getattr(report, "provider", "") or ""),
            "model": str(getattr(report, "model", "") or ""),
            "cases": cases,
            "trials": trials,
            "failed_cases": sum(
                1 for item in summary.values()
                if integer(item.get("failed", 0)) > 0),
            "error_count": (
                sum(int(value or 0) for value in error_summary.values())
                if isinstance(error_summary, Mapping) else 0),
            "contains_prompt_content": False,
        }

    def _live_identity(
        self, request: PromptPipelineRequest, report: object,
    ) -> dict[str, str]:
        profile_data = getattr(report, "profile", {}) or {}
        if not isinstance(profile_data, Mapping):
            profile_data = {}
        profile_name = (
            request.profile_name
            or str(profile_data.get("prompt_profile", "") or "")
        )
        if not profile_name:
            return {
                "error": "live-report-missing-profile-identity",
                "profile_name": "",
                "profile_hash": "",
                "suite_hash": "",
            }
        try:
            profile = self.release.profile(profile_name)
        except ValueError as exc:
            return {
                "error": f"unknown-profile:{exc}",
                "profile_name": profile_name,
                "profile_hash": "",
                "suite_hash": "",
            }
        report_release_id = str(
            profile_data.get("prompt_release_id", "") or "")
        report_release_hash = str(
            profile_data.get("prompt_release_hash", "") or "")
        report_profile_name = str(
            profile_data.get("prompt_profile", profile_name) or profile_name)
        report_profile_hash = str(
            profile_data.get("prompt_profile_hash", "") or "")
        suite_hash = str(profile_data.get("suite_hash", "") or "")
        if report_release_id and report_release_id != self.release.release_id:
            return {
                "error": "live-report-release-id-mismatch",
                "profile_name": profile.name,
                "profile_hash": profile.profile_hash,
                "suite_hash": suite_hash,
            }
        if report_release_hash and report_release_hash != self.release.release_hash:
            return {
                "error": "live-report-release-hash-mismatch",
                "profile_name": profile.name,
                "profile_hash": profile.profile_hash,
                "suite_hash": suite_hash,
            }
        if report_profile_name != profile.name:
            return {
                "error": "live-report-profile-name-mismatch",
                "profile_name": profile.name,
                "profile_hash": profile.profile_hash,
                "suite_hash": suite_hash,
            }
        if report_profile_hash and report_profile_hash != profile.profile_hash:
            return {
                "error": "live-report-profile-hash-mismatch",
                "profile_name": profile.name,
                "profile_hash": profile.profile_hash,
                "suite_hash": suite_hash,
            }
        if not suite_hash:
            return {
                "error": "live-report-missing-suite-hash",
                "profile_name": profile.name,
                "profile_hash": profile.profile_hash,
                "suite_hash": "",
            }
        return {
            "error": "",
            "profile_name": profile.name,
            "profile_hash": profile.profile_hash,
            "suite_hash": suite_hash,
        }

    def _finish(
        self,
        request: PromptPipelineRequest,
        candidate: PromptCandidate,
        stages: Sequence[PromptPipelineStage],
        *,
        state: PromptProposalState,
        authoring_report_hash: str = "",
        isolation: CandidateIsolationReport | None = None,
        offline_report_hash: str = "",
        live_report_hash: str = "",
        parity_report_hash: str = "",
        conflict_report_hash: str = "",
        profile_name: str = "",
        profile_hash: str = "",
        suite_hash: str = "",
        calibration: ModelProfileCalibration | None = None,
        issue: str = "",
    ) -> PromptReleaseProposal:
        stages_tuple = tuple(stages)
        if issue:
            stages_tuple = tuple(replace(
                stage,
                details={**dict(stage.details), "blocking_issue": issue},
            ) if stage.status is PromptPipelineStageStatus.PENDING else stage
                for stage in stages_tuple)
        evidence_id = _hash({
            "candidate_hash": candidate.candidate_hash,
            "authoring": authoring_report_hash,
            "isolation": (
                _hash(isolation.as_dict()) if isolation is not None else ""),
            "offline": offline_report_hash,
            "live": live_report_hash,
            "parity": parity_report_hash,
            "conflict": conflict_report_hash,
            "release": self.release.release_hash,
            "profile": profile_hash,
            "calibration": (
                calibration.calibration_hash
                if calibration is not None else ""),
        })
        proposal = PromptReleaseProposal(
            schema_version=PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION,
            proposal_id=f"prompt-release:{evidence_id[:32]}",
            created_at=self.clock(),
            candidate_hash=candidate.candidate_hash,
            candidate=candidate.content_light_record(),
            authoring_report_hash=authoring_report_hash,
            isolation_report_hash=(
                _hash(isolation.as_dict()) if isolation is not None else ""),
            offline_report_hash=offline_report_hash,
            live_report_hash=live_report_hash,
            parity_report_hash=parity_report_hash,
            conflict_report_hash=conflict_report_hash,
            release_id=self.release.release_id,
            release_hash=self.release.release_hash,
            profile_name=profile_name,
            profile_hash=profile_hash,
            contract_version=PROMPT_PARITY_CONTRACT.version,
            contract_hash=PROMPT_PARITY_CONTRACT.hash,
            suite_hash=suite_hash,
            calibration=calibration,
            stages=stages_tuple,
            state=state,
            requested_by=request.requested_by,
            skill_name=request.skill_name,
        )
        if self.store is not None:
            self.store.save_proposal(proposal)
        return proposal


class PromptReleaseApprovalService:
    """Human-only proposal decisions and local approval artifacts."""

    def __init__(
        self,
        store: PromptReleaseProposalStore,
        *,
        release: PromptRelease = CURRENT_PROMPT_RELEASE,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.store = store
        self.release = release
        self.clock = clock

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
        return self._decide(
            proposal_id,
            candidate_hash,
            decision="approved",
            approved_by=approved_by,
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
    "PromptReleaseApprovalService",
    "PromptReleasePipeline",
    "PromptReleaseProposal",
    "PromptReleaseProposalStore",
]
