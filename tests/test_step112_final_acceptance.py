"""Step 112: executable final acceptance and fail-closed Live Gate."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from krelcode.agent.prompts import resolve_prompt_profile
from krelcode.benchmarks import (
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    FINAL_ACCEPTANCE_CONTRACT,
    FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH,
    FROZEN_PROMPT_PARITY_CONTRACT_HASH,
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
    AcceptanceArea,
    AcceptanceCheckStatus,
    AcceptanceEvidenceKind,
    BehaviorCheck,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
    FinalAcceptanceContract,
    FinalAcceptanceRunner,
    FinalAcceptanceStatus,
    PromptParityBaseline,
    PromptParityStatus,
    evaluate_prompt_parity,
)
from krelcode.benchmarks import acceptance as acceptance_benchmark
from krelcode.benchmarks import behavior as benchmark_behavior
from krelcode.benchmarks import prompt as benchmark_prompt
from krelcode.llm.registry import ProviderSettings


_MODES = (
    "main",
    "headless",
    "stream-json",
    "sdk",
    "subagent",
    "resume",
    "fork",
    "rewind",
    "compact",
)
_METRICS = (
    "relevant_scope_only",
    "memory_not_instruction",
    "rule_precedence_stable",
    "subagent_history_isolated",
    "subagent_memory_evidence",
    "stable_prefix_isolated",
    "compact_recoverable",
    "provider_hash_exact",
)


class _OfflineReport:
    def __init__(self, *, passed: bool = True) -> None:
        self.passed = passed

    def mode_summary(self):
        return {
            mode: {
                "covered": True,
                "cases": 1,
                "passed": 1 if self.passed or mode != "main" else 0,
                "failed": 0 if self.passed or mode != "main" else 1,
            }
            for mode in _MODES
        }

    def metric_summary(self):
        return {
            metric: {
                "passed": self.passed,
                "checks": 1,
                "passed_checks": 1 if self.passed else 0,
                "failed_checks": 0 if self.passed else 1,
            }
            for metric in _METRICS
        }

    def as_dict(self):
        return {
            "kind": "offline_fixture",
            "passed": self.passed,
            "modes": self.mode_summary(),
            "metrics": self.metric_summary(),
            "contains_prompt_content": False,
        }


def _live_report(*, failed_case: str = "", generated_at: str | None = None):
    resolution = resolve_prompt_profile(
        "deepseek", "deepseek-chat", configured="deepseek")
    results = tuple(
        BehaviorTrialResult(
            case_id=case_id,
            trial=trial,
            passed=case_id != failed_case,
            checks=(BehaviorCheck(
                BehaviorMetric.TASK_SUCCESS,
                case_id != failed_case,
                "synthetic final acceptance fixture",
            ),),
            usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            duration_ms=1,
        )
        for case_id in PROMPT_PARITY_CONTRACT.required_case_ids
        for trial in range(1, PROMPT_PARITY_CONTRACT.minimum_trials + 1)
    )
    return BehaviorReport(
        schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        provider="deepseek",
        model="deepseek-chat",
        passed=not failed_case,
        results=results,
        duration_ms=1,
        profile={
            "krelcode_version": "test",
            "suite_hash": FROZEN_PROMPT_PARITY_SUITE_HASH,
            "sandbox_backend": "static",
            "max_steps": 14,
            "context_max_tokens": 30000,
            "trial_timeout_seconds": 300.0,
            "parity_contract_version": PROMPT_PARITY_CONTRACT.version,
            "parity_contract_hash": FROZEN_PROMPT_PARITY_CONTRACT_HASH,
            "prompt_release_id": resolution.release_id,
            "prompt_release_hash": resolution.release_hash,
            "prompt_profile": resolution.name,
            "prompt_profile_hash": resolution.profile_hash,
            "profile_calibration_status": "uncalibrated",
        },
    )


def _live_gate(report=None):
    accepted = _live_report()
    baseline = PromptParityBaseline.from_report(accepted)
    return evaluate_prompt_parity(report or accepted, baseline)


def _check(report, check_id):
    return next(item for item in report.checks if item.id == check_id)


def test_final_acceptance_contract_is_frozen_and_covers_every_area():
    assert FINAL_ACCEPTANCE_CONTRACT.hash == FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH
    assert len(FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH) == 64
    assert len(FINAL_ACCEPTANCE_CONTRACT.requirements) == 17
    assert {
        requirement.area for requirement in FINAL_ACCEPTANCE_CONTRACT.requirements
    } == set(AcceptanceArea)
    evidence_kinds = {
        requirement.evidence_kind
        for requirement in FINAL_ACCEPTANCE_CONTRACT.requirements
    }
    assert evidence_kinds == {
        AcceptanceEvidenceKind.RUNTIME,
        AcceptanceEvidenceKind.OFFLINE,
        AcceptanceEvidenceKind.LIVE,
    }
    for requirement in FINAL_ACCEPTANCE_CONTRACT.requirements:
        for relative in (*requirement.artifacts, *requirement.test_refs):
            assert Path(relative).is_file(), (requirement.id, relative)


def test_offline_matrix_is_pending_live_and_does_not_rerun_an_injected_report():
    runner = FinalAcceptanceRunner(offline_report=_OfflineReport())

    def fail(_kwargs):
        raise AssertionError("injected offline report must not run twice")

    runner._run_offline = fail
    report = runner.run()

    assert report.status is FinalAcceptanceStatus.PENDING_LIVE
    assert report.exit_code == 2
    assert report.summary() == {"checks": 18, "passed": 17, "failed": 0, "pending": 1}
    live = _check(report, "live-model-parity-gate")
    assert live.status is AcceptanceCheckStatus.PENDING


def test_failed_offline_matrix_fails_closed():
    report = FinalAcceptanceRunner(
        offline_report=_OfflineReport(passed=False),
    ).run()

    assert report.status is FinalAcceptanceStatus.FAILED
    assert report.exit_code == 1
    content = _check(report, "content-light-final-report")
    assert content.status is AcceptanceCheckStatus.FAILED
    assert content.evidence["content_light_violations"] == 0


def test_fresh_full_live_gate_is_accepted_and_fingerprinted():
    live_report = _live_report()
    gate = _live_gate(live_report)
    assert gate.passed and gate.status is PromptParityStatus.ALIGNED
    report = FinalAcceptanceRunner(
        offline_report=_OfflineReport(),
    ).run(live_gate=gate, live_report=live_report)

    assert report.status is FinalAcceptanceStatus.ACCEPTED
    assert report.exit_code == 0
    assert report.summary()["failed"] == 0
    assert report.summary()["pending"] == 0
    assert report.fingerprints["prompt_release_hash"]
    assert report.fingerprints["prompt_parity_contract_hash"] == (
        FROZEN_PROMPT_PARITY_CONTRACT_HASH)
    assert report.fingerprints["prompt_parity_suite_hash"] == (
        FROZEN_PROMPT_PARITY_SUITE_HASH)
    assert report.fingerprints["live_report_suite_hash"] == (
        FROZEN_PROMPT_PARITY_SUITE_HASH)
    assert len(report.fingerprints["live_report_hash"]) == 64
    assert len(report.fingerprints["live_gate_hash"]) == 64
    assert _check(report, "live-model-parity-gate").status is (
        AcceptanceCheckStatus.PASSED)


def test_live_failure_stale_evidence_and_missing_source_report_fail_closed():
    good = _live_report()
    failed_live = _live_report(failed_case="read-scope")
    failed_gate = _live_gate(failed_live)
    failed = FinalAcceptanceRunner(offline_report=_OfflineReport()).run(
        live_gate=failed_gate,
        live_report=failed_live,
    )
    assert failed.status is FinalAcceptanceStatus.FAILED

    stale_live = _live_report(generated_at="2020-01-01T00:00:00+00:00")
    stale_gate = _live_gate(good)
    stale = FinalAcceptanceRunner(offline_report=_OfflineReport()).run(
        live_gate=stale_gate,
        live_report=stale_live,
    )
    assert stale.status is FinalAcceptanceStatus.FAILED
    stale_check = _check(stale, "live-model-parity-gate")
    assert "stale-live-evidence" in stale_check.evidence["issues"]

    missing_source = FinalAcceptanceRunner(
        offline_report=_OfflineReport(),
    ).run(live_gate=stale_gate)
    assert missing_source.status is FinalAcceptanceStatus.FAILED
    assert _check(
        missing_source, "live-model-parity-gate"
    ).status is AcceptanceCheckStatus.FAILED


def test_missing_artifacts_and_contract_drift_fail_closed(tmp_path):
    missing = FinalAcceptanceRunner(workspace=tmp_path)._contract_check()
    assert missing.status is AcceptanceCheckStatus.FAILED
    assert missing.evidence["missing_files"]

    first = replace(
        FINAL_ACCEPTANCE_CONTRACT.requirements[0],
        title="drifted acceptance requirement",
    )
    changed_contract = FinalAcceptanceContract(
        schema_version=FINAL_ACCEPTANCE_CONTRACT.schema_version,
        version=FINAL_ACCEPTANCE_CONTRACT.version,
        requirements=(
            first,
            *FINAL_ACCEPTANCE_CONTRACT.requirements[1:],
        ),
    )
    drift = FinalAcceptanceRunner(
        contract=changed_contract,
        offline_report=_OfflineReport(),
    )._contract_check()
    assert drift.status is AcceptanceCheckStatus.FAILED
    assert not drift.evidence["frozen_hash_matches"]


def test_report_self_check_redacts_prompt_and_credential_content(monkeypatch):
    runner = FinalAcceptanceRunner(offline_report=_OfflineReport())
    secret = "sk-" + "a" * 24
    runner._probe_provider = lambda: (
        True,
        "Managed benchmark policy should never be emitted",
        {"api_key": secret},
    )
    report = runner.run()
    encoded = json.dumps(report.as_dict(), ensure_ascii=False)

    assert report.status is FinalAcceptanceStatus.FAILED
    assert "Managed benchmark policy" not in encoded
    assert secret not in encoded
    assert '"api_key"' not in encoded
    assert report.as_dict()["contains_prompt_content"] is False
    content = _check(report, "content-light-final-report")
    assert content.status is AcceptanceCheckStatus.FAILED
    assert content.evidence["content_light_violations"] >= 1


def test_provider_messages_has_one_production_construction_site():
    offenders = []
    for path in Path("src/krelcode").rglob("*.py"):
        if path.as_posix().endswith("agent/prompt_assembler.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else ""
            )
            if name == "ProviderMessages":
                offenders.append(path.as_posix())
    assert offenders == []


def test_acceptance_cli_json_and_text_are_content_light(monkeypatch, capsys):
    class FakeAcceptanceReport:
        exit_code = 2

        def as_dict(self):
            return {
                "kind": "final_acceptance",
                "status": "pending_live",
                "contains_prompt_content": False,
                "summary": {"checks": 18, "passed": 17, "failed": 0, "pending": 1},
            }

        def render_text(self):
            return "Krelcode Final Acceptance · pending_live"

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self, **kwargs):
            return FakeAcceptanceReport()

    monkeypatch.setattr(acceptance_benchmark, "FinalAcceptanceRunner", FakeRunner)
    rc = acceptance_benchmark.main(["--json", "--no-sdk"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["status"] == "pending_live"
    assert payload["contains_prompt_content"] is False

    rc = acceptance_benchmark.main(["--no-sdk"])
    output = capsys.readouterr().out
    assert rc == 2
    assert "pending_live" in output


def test_prompt_cli_final_acceptance_requires_fresh_gate_and_content_light(
    tmp_path, monkeypatch, capsys,
):
    live_report = _live_report()
    baseline = PromptParityBaseline.from_report(live_report)
    baseline_path = tmp_path / "baseline.json"
    from krelcode.benchmarks import save_prompt_baseline

    save_prompt_baseline(baseline_path, baseline)

    class FakeBehaviorRunner:
        def __init__(self, factory, **kwargs):
            self.kwargs = kwargs

        def run(self, case_ids=None):
            assert case_ids is None
            return live_report

    class FakeAcceptance:
        exit_code = 0

        def as_dict(self):
            return {
                "kind": "final_acceptance",
                "status": "accepted",
                "contains_prompt_content": False,
            }

        def render_text(self):
            return "Krelcode Final Acceptance · accepted"

    captured = {}

    class FakeFinalRunner:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def run(self, **kwargs):
            captured["run"] = kwargs
            return FakeAcceptance()

    class Config:
        sources = ("test",)
        data = {}

    monkeypatch.setattr(
        benchmark_behavior, "load_config_layers", lambda **kwargs: Config())
    monkeypatch.setattr(
        benchmark_behavior,
        "resolve_settings",
        lambda **kwargs: ProviderSettings(
            provider="deepseek", model="deepseek-chat", api_key="test-key"),
    )
    monkeypatch.setattr(
        benchmark_behavior, "BehaviorBenchmarkRunner", FakeBehaviorRunner)
    monkeypatch.setattr(
        acceptance_benchmark, "FinalAcceptanceRunner", FakeFinalRunner)

    rc = benchmark_prompt.main([
        "--live",
        "--trials", "1",
        "--gate", str(baseline_path),
        "--final-acceptance",
        "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["final_acceptance"]["status"] == "accepted"
    assert captured["run"]["live_report"] is live_report
    assert captured["run"]["live_gate"].passed

    rc = benchmark_prompt.main(["--final-acceptance", "--json"])
    assert rc == 2
    assert "必须与 --live" in capsys.readouterr().err

    rc = benchmark_prompt.main([
        "--release", "--final-acceptance", "--json",
    ])
    assert rc == 2
    assert "不能与 contract/release/list 混用" in capsys.readouterr().err

    rc = benchmark_prompt.main([
        "--live", "--gate", str(baseline_path),
        "--final-acceptance", "--include-content", "--json",
    ])
    assert rc == 2
    assert "content-light" in capsys.readouterr().err

    rc = benchmark_prompt.main([
        "--live", "--gate", str(baseline_path), "--case", "read-scope",
        "--final-acceptance", "--json",
    ])
    assert rc == 2
    assert "完整 case manifest" in capsys.readouterr().err
