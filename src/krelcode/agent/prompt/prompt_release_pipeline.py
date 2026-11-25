"""End-to-end Candidate Prompt release proposal pipeline.

Evaluation orchestration lives here; proposal DTOs, persistence and the
human-only approval service are exposed through this historical import path.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Callable, Mapping, Optional, Sequence

from krelcode.agent.prompt.fragments import PromptFragmentRegistry
from krelcode.agent.prompt.prompt_authoring import PromptAuthoringValidator
from krelcode.agent.prompt.prompt_candidate import PromptCandidate
from krelcode.agent.prompt.prompt_conflicts import PromptConflictAnalyzer
from krelcode.agent.prompt.prompt_isolation import (
    CandidateIsolationReport,
    PromptCandidateIsolator,
)
from krelcode.agent.prompt.prompt_parity import (
    PROMPT_PARITY_CONTRACT,
    PromptParityGateResult,
    evaluate_prompt_parity,
)
from krelcode.agent.prompt.prompt_release import ModelProfileCalibration, PromptRelease
from krelcode.agent.prompt.prompt_release_models import (
    PROMPT_HUMAN_APPROVAL_KIND,
    PROMPT_RELEASE_PIPELINE_SCHEMA_VERSION,
    PROMPT_RELEASE_PROPOSAL_KIND,
    PromptHumanApproval,
    PromptPipelineError,
    PromptPipelineRequest,
    PromptPipelineStage,
    PromptPipelineStageStatus,
    PromptProposalState,
    PromptReleaseProposal,
    _hash,
    _now,
)
from krelcode.agent.prompt.prompt_release_store import PromptReleaseProposalStore
from krelcode.agent.prompt.prompt_release_support import (
    case_summary,
    integer,
    mapping,
    mapping_items,
)
from krelcode.agent.prompts import CURRENT_PROMPT_RELEASE


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



# Imported last so the approval service can depend on the focused modules
# without creating an implementation cycle.
from krelcode.agent.prompt.prompt_release_approval import PromptReleaseApprovalService


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
