"""Stable tuning/benchmark boundary over production Harness ports."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.change_admission import (
    AdmissionDecision,
    AdmissionGatePort,
    ChangeApproval,
    ChangeCandidate,
    EvaluationCheck,
    EvaluationReport,
    RollbackManifest,
)
from ..contracts.evaluation import (
    BehaviorFixture,
    BehaviorFixturePort,
    EvaluationHarnessPort,
    TraceReplayPort,
    VersionManifestPort,
)


@dataclass(frozen=True)
class BenchmarkPorts:
    harness: EvaluationHarnessPort
    fixtures: BehaviorFixturePort
    trace_replay: TraceReplayPort
    versions: VersionManifestPort


async def evaluate_candidate(
    ports: BenchmarkPorts,
    candidate: ChangeCandidate,
    *,
    fixture_ids: tuple[str, ...] = (),
) -> tuple[EvaluationReport, ...]:
    """Run the requested behavior fixtures through the production Harness."""
    fixtures = (
        tuple(ports.fixtures.get_fixture(item) for item in fixture_ids)
        if fixture_ids else tuple(ports.fixtures.list_fixtures())
    )
    reports = []
    for fixture in fixtures:
        report = await ports.harness.evaluate(fixture, candidate)
        if report.candidate_id != candidate.candidate_id:
            raise ValueError(
                "EvaluationHarnessPort 返回了其他候选变更的报告")
        reports.append(report)
    return tuple(reports)


async def evaluate_and_admit(
    ports: BenchmarkPorts,
    gate: AdmissionGatePort,
    candidate: ChangeCandidate,
    *,
    fixture_ids: tuple[str, ...] = (),
    approval: ChangeApproval | None = None,
    rollback: RollbackManifest | None = None,
    policy_version: str = "",
) -> AdmissionDecision:
    """Run production fixtures, aggregate checks, then request admission."""
    reports = await evaluate_candidate(
        ports, candidate, fixture_ids=fixture_ids)
    checks: list[EvaluationCheck] = []
    for report in reports:
        for check in report.checks:
            checks.append(EvaluationCheck(
                name=f"{report.candidate_id}:{check.name}",
                passed=check.passed,
                reason=check.reason,
                required=check.required,
                score=check.score,
                threshold=check.threshold,
                evidence=check.evidence,
            ))
    evaluation = EvaluationReport.from_checks(
        candidate.candidate_id,
        checks,
        metadata={"fixture_reports": len(reports)},
    )
    return gate.admit(
        candidate,
        evaluation=evaluation,
        approval=approval,
        rollback=rollback,
        policy_version=policy_version,
    )


__all__ = [
    "BehaviorFixture",
    "BehaviorFixturePort",
    "BenchmarkPorts",
    "evaluate_and_admit",
    "EvaluationHarnessPort",
    "TraceReplayPort",
    "VersionManifestPort",
    "evaluate_candidate",
]
