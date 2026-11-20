"""Step 107: frozen Prompt Parity contract and content-light benchmark gate."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from krelcode.benchmarks import (
    BEHAVIOR_CASES,
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    FROZEN_PROMPT_PARITY_CONTRACT_HASH,
    PROMPT_PARITY_CONTRACT,
    PROMPT_PARITY_MINIMUM_TRIALS,
    BehaviorCheck,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
    ParityDimension,
    PromptParityBaseline,
    PromptParityStatus,
    contract_summary,
    evaluate_prompt_parity,
    load_prompt_baseline,
    save_prompt_baseline,
)
from krelcode.benchmarks import behavior as benchmark_behavior
from krelcode.benchmarks import prompt as benchmark_prompt
from krelcode.llm.registry import ProviderSettings


def _full_report(
    *,
    trials: int = PROMPT_PARITY_MINIMUM_TRIALS,
    failed_case: str = "",
) -> BehaviorReport:
    results = []
    for case_id in PROMPT_PARITY_CONTRACT.required_case_ids:
        for trial in range(1, trials + 1):
            passed = case_id != failed_case
            results.append(BehaviorTrialResult(
                case_id=case_id,
                trial=trial,
                passed=passed,
                checks=(BehaviorCheck(
                    BehaviorMetric.TASK_SUCCESS,
                    passed,
                    "synthetic parity fixture",
                ),),
                usage={
                    "calls": 1,
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                },
            ))
    return BehaviorReport(
        schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at="2026-11-20T00:00:00+00:00",
        provider="scripted",
        model="scripted-model",
        passed=not failed_case,
        results=tuple(results),
        duration_ms=1,
        profile={
            "krelcode_version": "test",
            "suite_hash": "a" * 64,
            "sandbox_backend": "static",
            "max_steps": 14,
            "context_max_tokens": 30000,
            "trial_timeout_seconds": 300.0,
        },
    )


def test_contract_is_frozen_and_covers_every_dimension_and_case():
    assert PROMPT_PARITY_CONTRACT.hash == FROZEN_PROMPT_PARITY_CONTRACT_HASH
    assert len(PROMPT_PARITY_CONTRACT.hash) == 64
    assert PROMPT_PARITY_CONTRACT.minimum_trials == 3
    assert len(PROMPT_PARITY_CONTRACT.required_case_ids) == 16
    assert tuple(
        case.id for case in BEHAVIOR_CASES
    ) == PROMPT_PARITY_CONTRACT.required_case_ids
    assert {
        requirement.dimension for requirement in PROMPT_PARITY_CONTRACT.requirements
    } == set(ParityDimension)
    summary = contract_summary()
    assert summary["contract_hash"] == FROZEN_PROMPT_PARITY_CONTRACT_HASH
    assert set(summary["dimensions"]) == {
        dimension.value for dimension in ParityDimension
    }


def test_baseline_roundtrip_is_content_light_and_hash_verified(tmp_path):
    baseline = PromptParityBaseline.from_report(_full_report())
    path = tmp_path / "baseline.json"
    save_prompt_baseline(path, baseline)
    loaded = load_prompt_baseline(path)

    assert loaded.hash == baseline.hash
    assert loaded.contract_hash == PROMPT_PARITY_CONTRACT.hash
    assert len(loaded.cases) == 16
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["contains_prompt_content"] is False
    encoded = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "synthetic parity fixture",
        "system_prompt",
        "answer",
        "tool_arguments",
        "api_key",
    ):
        assert forbidden not in encoded


def test_baseline_rejects_partial_or_under_sampled_reports():
    report = _full_report(trials=1)
    with pytest.raises(ValueError, match="至少需要 3 trials"):
        PromptParityBaseline.from_report(report)

    original = _full_report()
    partial = replace(
        original,
        results=tuple(
            result for result in original.results
            if result.case_id != "read-scope"
        ),
    )
    with pytest.raises(ValueError, match="完整 case manifest"):
        PromptParityBaseline.from_report(partial)


def test_gate_passes_complete_stable_three_trial_suite():
    report = _full_report()
    baseline = PromptParityBaseline.from_report(report)
    gate = evaluate_prompt_parity(report, baseline)

    assert gate.passed
    assert gate.status is PromptParityStatus.ALIGNED
    assert gate.contract_hash == FROZEN_PROMPT_PARITY_CONTRACT_HASH
    assert all(check.passed for check in gate.checks)
    assert gate.as_dict()["summary"] == {
        "checks": len(gate.checks),
        "passed_checks": len(gate.checks),
        "failed_checks": 0,
    }


def test_gate_fails_closed_on_regression_and_trial_floor():
    passing = _full_report()
    baseline = PromptParityBaseline.from_report(passing)
    regressed = _full_report(failed_case="read-scope")
    gate = evaluate_prompt_parity(regressed, baseline)
    checks = {check.id: check.passed for check in gate.checks}

    assert not gate.passed
    assert gate.status is PromptParityStatus.NOT_ALIGNED
    assert not checks["case-regressions"]
    assert not checks["report-passed"]

    one_trial = _full_report(trials=1)
    floor_gate = evaluate_prompt_parity(one_trial, baseline)
    floor_checks = {check.id: check.passed for check in floor_gate.checks}
    assert not floor_gate.passed
    assert not floor_checks["trial-floor"]


def test_gate_rejects_contract_mismatch_and_unknown_baseline_fields(tmp_path):
    report = _full_report()
    baseline = PromptParityBaseline.from_report(report)
    stale = replace(baseline, contract_hash="b" * 64)
    gate = evaluate_prompt_parity(report, stale)
    assert not gate.passed
    assert not {check.id: check.passed for check in gate.checks}["baseline-contract"]

    old_schema = replace(baseline, benchmark_schema_version=2)
    schema_gate = evaluate_prompt_parity(report, old_schema)
    assert not schema_gate.passed
    assert not {
        check.id: check.passed for check in schema_gate.checks
    }["benchmark-schema"]

    path = tmp_path / "baseline.json"
    save_prompt_baseline(path, baseline)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["prompt_body"] = "must be rejected"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="不受支持的字段"):
        load_prompt_baseline(path)

    save_prompt_baseline(path, baseline)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["cases"][0]["tool_arguments"] = {"api_key": "must be rejected"}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="baseline case 包含不受支持的字段"):
        load_prompt_baseline(path)


def test_cli_exposes_frozen_contract_without_credentials(capsys):
    rc = benchmark_prompt.main(["--contract", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["contract_hash"] == FROZEN_PROMPT_PARITY_CONTRACT_HASH
    assert len(payload["cases"]) == 16
    assert payload["minimum_trials"] == 3


def test_cli_saves_and_enforces_content_light_baseline(tmp_path, monkeypatch, capsys):
    report = _full_report()

    class FakeRunner:
        instances = []

        def __init__(self, factory, **kwargs):
            self.factory = factory
            self.kwargs = kwargs
            self.__class__.instances.append(self)

        def run(self, case_ids=None):
            assert case_ids is None
            return report

    class Config:
        sources = ("test",)
        data = {}

    monkeypatch.setattr(
        benchmark_behavior, "load_config_layers", lambda **kwargs: Config())
    monkeypatch.setattr(
        benchmark_behavior,
        "resolve_settings",
        lambda **kwargs: ProviderSettings(
            provider="scripted", model="scripted-model", api_key="test-key"),
    )
    monkeypatch.setattr(
        benchmark_behavior, "BehaviorBenchmarkRunner", FakeRunner)

    baseline_path = tmp_path / "baseline.json"
    rc = benchmark_prompt.main([
        "--live", "--trials", "1", "--json",
        "--save-baseline", str(baseline_path),
    ])
    saved = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert saved["baseline_path"] == str(baseline_path)
    assert baseline_path.is_file()
    assert FakeRunner.instances[-1].kwargs["trials"] == 3

    rc = benchmark_prompt.main([
        "--live", "--json", "--gate", str(baseline_path),
    ])
    gated = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert gated["gate"]["passed"] is True
    assert gated["gate"]["status"] == "aligned"
    assert FakeRunner.instances[-1].kwargs["trials"] == 3

    rc = benchmark_prompt.main([
        "--live", "--json", "--include-content",
        "--gate", str(baseline_path),
    ])
    assert rc == 2
    assert "content-light" in capsys.readouterr().err
