"""Universal hot-change admission and stable benchmark ports."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from krelcode.benchmarks.ports import (
    BenchmarkPorts,
    evaluate_and_admit,
    evaluate_candidate,
)
from krelcode.contracts.application import RiskLevel
from krelcode.contracts.change_admission import (
    AdmissionStatus,
    ChangeApproval,
    ChangeCandidate,
    ChangeType,
    EvaluationCheck,
    EvaluationReport,
    RollbackManifest,
)
from krelcode.contracts.evaluation import BehaviorFixture
from krelcode.governance.change_pipeline import DefaultAdmissionGate
from krelcode.governance.control_plane.helpers import governance_for_change


def candidate(
    change_type: ChangeType = ChangeType.MODEL_PROFILE,
    *,
    risk: RiskLevel = RiskLevel.HIGH,
) -> ChangeCandidate:
    return ChangeCandidate(
        change_type=change_type,
        target="model:test",
        before={"model": "old"},
        after={"model": "new"},
        risk=risk,
        affected_artifacts=(
            {"kind": "model_profile", "id": "new"},
        ),
    )


def passing_report(item: ChangeCandidate) -> EvaluationReport:
    return EvaluationReport.from_checks(
        item.candidate_id,
        [EvaluationCheck(
            name="behavior", passed=True, score=1.0)],
    )


def test_default_admission_gate_enforces_all_high_risk_steps() -> None:
    gate = DefaultAdmissionGate()
    item = candidate()
    report = passing_report(item)
    governance = governance_for_change(item)

    missing_evaluation = gate.admit(
        item, governance_decision=governance)
    assert missing_evaluation.status is AdmissionStatus.REJECTED
    assert "EvaluationReport" in missing_evaluation.reason

    missing_rollback = gate.admit(
        item, evaluation=report, governance_decision=governance)
    assert missing_rollback.status is AdmissionStatus.REJECTED
    assert "RollbackManifest" in missing_rollback.reason

    rollback = RollbackManifest(
        candidate_id=item.candidate_id,
        snapshot={"model": "old"},
    )
    pending = gate.admit(
        item, evaluation=report, rollback=rollback,
        governance_decision=governance)
    assert pending.status is AdmissionStatus.PENDING_APPROVAL

    admitted = gate.admit(
        item,
        evaluation=report,
        rollback=rollback,
        approval=ChangeApproval(
            approved=True, actor="user", reason="explicit /model"),
        governance_decision=governance,
    )
    assert admitted.status is AdmissionStatus.ADMITTED
    assert admitted.release_id.startswith("admission-")
    assert admitted.governance_decision is not None
    assert admitted.governance_decision.allowed
    assert admitted.governance_decision.audit.request_id == item.candidate_id


def test_failed_evaluation_and_denied_approval_are_rejected() -> None:
    gate = DefaultAdmissionGate()
    item = candidate()
    governance = governance_for_change(item)
    failed = EvaluationReport.from_checks(
        item.candidate_id,
        [EvaluationCheck(
            name="behavior", passed=False, reason="regression")],
    )
    rollback = RollbackManifest(
        candidate_id=item.candidate_id,
        snapshot={"model": "old"},
    )
    rejected = gate.admit(
        item, evaluation=failed, rollback=rollback,
        governance_decision=governance)
    assert rejected.status is AdmissionStatus.REJECTED
    assert "评估未通过" in rejected.reason

    denied = gate.admit(
        item,
        evaluation=passing_report(item),
        rollback=rollback,
        approval=ChangeApproval(
            approved=False, actor="user", reason="not allowed"),
        governance_decision=governance,
    )
    assert denied.status is AdmissionStatus.REJECTED
    assert "not allowed" in denied.reason


@pytest.mark.parametrize("change_type", list(ChangeType))
def test_every_change_type_has_an_admission_policy(
    change_type: ChangeType,
) -> None:
    gate = DefaultAdmissionGate()
    item = candidate(change_type, risk=RiskLevel.LOW)
    decision = gate.admit(
        item,
        evaluation=passing_report(item),
        governance_decision=governance_for_change(item),
    )
    assert decision.status is AdmissionStatus.ADMITTED


class Fixtures:
    def list_fixtures(self):
        return (self.get_fixture("one"), self.get_fixture("two"))

    def get_fixture(self, fixture_id: str) -> BehaviorFixture:
        return BehaviorFixture(fixture_id=fixture_id, task=f"task-{fixture_id}")


class Harness:
    def __init__(self) -> None:
        self.calls = []

    async def evaluate(self, fixture, item):
        self.calls.append((fixture.fixture_id, item.candidate_id))
        return passing_report(item)


class TraceReplay:
    async def replay(self, trace, fixture):
        return {"trace": dict(trace), "fixture": fixture.fixture_id}


class Versions:
    def current(self):
        return {"release_id": "runtime-1"}

    def diff(self, left_release_id, right_release_id):
        return {"left": left_release_id, "right": right_release_id}


def test_benchmark_runner_only_uses_stable_ports() -> None:
    harness = Harness()
    ports = BenchmarkPorts(
        harness=harness,
        fixtures=Fixtures(),
        trace_replay=TraceReplay(),
        versions=Versions(),
    )
    item = candidate()
    reports = asyncio.run(evaluate_candidate(
        ports, item, fixture_ids=("one", "two")))

    assert len(reports) == 2
    assert harness.calls == [
        ("one", item.candidate_id),
        ("two", item.candidate_id),
    ]
    assert ports.versions.current()["release_id"] == "runtime-1"


def test_benchmark_evaluation_feeds_universal_admission_gate() -> None:
    ports = BenchmarkPorts(
        harness=Harness(),
        fixtures=Fixtures(),
        trace_replay=TraceReplay(),
        versions=Versions(),
    )
    item = candidate()
    decision = asyncio.run(evaluate_and_admit(
        ports,
        DefaultAdmissionGate(),
        item,
        approval=ChangeApproval(
            approved=True, actor="benchmark-operator"),
        rollback=RollbackManifest(
            candidate_id=item.candidate_id,
            snapshot={"model": "old"},
        ),
        governance_decision=governance_for_change(item),
    ))

    assert decision.status is AdmissionStatus.ADMITTED
    assert decision.evaluation is not None
    assert len(decision.evaluation.checks) == 2
