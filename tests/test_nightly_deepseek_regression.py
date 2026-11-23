"""DeepSeek live baseline freeze and nightly regression safeguards."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

from krelcode.agent.prompts import resolve_prompt_profile
from krelcode.benchmarks import (
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
    BehaviorCheck,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
    PromptParityBaseline,
    evaluate_prompt_parity,
)
from krelcode.benchmarks import behavior as benchmark_behavior
from krelcode.benchmarks.nightly import render_nightly_summary
from krelcode.llm.registry import ProviderSettings


ROOT = Path(__file__).resolve().parents[1]


def _report() -> BehaviorReport:
    resolution = resolve_prompt_profile("openai", "deepseek-flash")
    results = tuple(
        BehaviorTrialResult(
            case_id=case_id,
            trial=trial,
            passed=True,
            checks=(
                BehaviorCheck(BehaviorMetric.TASK_SUCCESS, True, "fixture"),
            ),
            usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        )
        for case_id in PROMPT_PARITY_CONTRACT.required_case_ids
        for trial in range(1, PROMPT_PARITY_CONTRACT.minimum_trials + 1)
    )
    return BehaviorReport(
        schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at="2026-11-23T00:00:00+00:00",
        provider="openai",
        model="deepseek-flash",
        passed=True,
        results=results,
        duration_ms=1,
        profile={
            "krelcode_version": "test",
            "suite_hash": FROZEN_PROMPT_PARITY_SUITE_HASH,
            "parity_contract_version": PROMPT_PARITY_CONTRACT.version,
            "parity_contract_hash": PROMPT_PARITY_CONTRACT.hash,
            "sandbox_backend": "static",
            "max_steps": 14,
            "context_max_tokens": 30000,
            "trial_timeout_seconds": 300.0,
            "provider_base_url_hash": "a" * 64,
            "provider_generation_hash": "b" * 64,
            "provider_timeout_seconds": 120.0,
            "provider_max_tokens": 0,
            "prompt_release_id": resolution.release_id,
            "prompt_release_hash": resolution.release_hash,
            "prompt_profile": resolution.name,
            "prompt_profile_hash": resolution.profile_hash,
            "profile_calibration_status": "uncalibrated",
        },
    )


def test_provider_execution_profile_hashes_without_persisting_endpoint_or_key():
    settings = ProviderSettings(
        provider="openai",
        model="deepseek-flash",
        base_url="https://private.example.test/v1",
        api_key="sk-must-not-leak",
        timeout=91.0,
        extra={"generation": {"temperature": 0.0, "top_p": 0.8}},
    )
    profile = benchmark_behavior._provider_execution_profile(settings)
    encoded = json.dumps(profile, sort_keys=True)

    assert len(profile["provider_base_url_hash"]) == 64
    assert len(profile["provider_generation_hash"]) == 64
    assert profile["provider_timeout_seconds"] == 91.0
    assert "private.example" not in encoded
    assert "sk-must-not-leak" not in encoded

    changed = replace(
        settings,
        extra={"generation": {"temperature": 0.2, "top_p": 0.8}},
    )
    changed_profile = benchmark_behavior._provider_execution_profile(changed)
    assert (
        changed_profile["provider_generation_hash"]
        != profile["provider_generation_hash"]
    )


def test_parity_gate_rejects_frozen_runner_profile_drift():
    report = _report()
    baseline = PromptParityBaseline.from_report(report)
    assert evaluate_prompt_parity(report, baseline).passed

    changed_profile = dict(report.profile)
    changed_profile["provider_generation_hash"] = "c" * 64
    changed = replace(report, profile=changed_profile)
    gate = evaluate_prompt_parity(changed, baseline)
    checks = {check.id: check.passed for check in gate.checks}

    assert not gate.passed
    assert not checks["runner-profile"]


def test_nightly_summary_is_bounded_and_content_light(tmp_path):
    result = {
        "benchmark": {
            "kind": "live_model_behavior",
            "provider": "openai",
            "model": "deepseek-flash",
            "passed": True,
            "summary": {
                "cases": 16,
                "stable_cases": 16,
                "trials": 48,
                "passed_trials": 48,
                "pass_at_k": 16,
                "tool_calls": 44,
                "total_tokens": 12345,
                "cost_usd": 0.12,
                "errors": {},
            },
            "cases": {},
            "results": [{"answer": "MUST NOT RENDER"}],
        },
        "gate": {
            "passed": True,
            "status": "aligned",
            "baseline_hash": "a" * 64,
            "report_suite_hash": "b" * 64,
            "checks": [{"id": "runner-profile", "passed": True}],
        },
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")

    summary = render_nightly_summary(path)

    assert "Overall: **PASS**" in summary
    assert "16/16 stable" in summary
    assert "48/48 passed" in summary
    assert "Parity gate: PASS" in summary
    assert "MUST NOT RENDER" not in summary
    assert "content-light" in summary


def test_deepseek_nightly_config_and_workflow_pin_safe_execution():
    config_path = ROOT / "benchmarks" / "deepseek-live.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    provider = config["providers"]["openai"]

    assert provider["model"] == "deepseek-flash"
    assert provider["base_url"] == "https://api.deepseek.com/v1"
    assert provider["api_key_env"] == "DEEPSEEK_API_KEY"
    assert provider["generation"]["temperature"] == 0.0
    assert "api_key" not in provider

    workflow = (
        ROOT / ".github" / "workflows" / "nightly-deepseek-live.yml"
    ).read_text(encoding="utf-8")
    assert "schedule:" in workflow
    assert "secrets.DEEPSEEK_API_KEY" in workflow
    assert "benchmarks/baselines/deepseek-flash.json" in workflow
    assert "tests/test_nightly_deepseek_regression.py" in workflow
    assert "--suite nightly" in workflow
    assert "--save-baseline" in workflow
    assert "--gate" in workflow
