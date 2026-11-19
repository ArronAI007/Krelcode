"""P2: content-light Prompt/Context benchmark acceptance suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from krelcode.benchmarks import (
    BENCHMARK_CASES,
    PROMPT_BENCHMARK_SCHEMA_VERSION,
    PromptBenchmarkMetric,
    PromptBenchmarkMode,
    PromptBenchmarkRunner,
    main,
)
from krelcode.benchmarks import prompt as benchmark_prompt


@pytest.fixture(scope="module")
def benchmark_report():
    return PromptBenchmarkRunner().run()


def test_catalog_covers_every_prompt_benchmark_mode():
    assert len(BENCHMARK_CASES) == len(PromptBenchmarkMode) == 9
    assert {case.mode for case in BENCHMARK_CASES} == set(PromptBenchmarkMode)
    assert {case.id for case in BENCHMARK_CASES} == {
        mode.value for mode in PromptBenchmarkMode
    }
    assert len({case.id for case in BENCHMARK_CASES}) == len(BENCHMARK_CASES)


def test_full_benchmark_passes_every_execution_surface(benchmark_report):
    assert benchmark_report.passed
    assert benchmark_report.schema_version == PROMPT_BENCHMARK_SCHEMA_VERSION
    assert len(benchmark_report.results) == len(PromptBenchmarkMode)

    modes = benchmark_report.mode_summary()
    assert set(modes) == {mode.value for mode in PromptBenchmarkMode}
    assert all(item["covered"] and item["failed"] == 0 for item in modes.values())
    assert all(result.passed for result in benchmark_report.results)


def test_benchmark_exercises_all_eight_acceptance_metrics(benchmark_report):
    metrics = benchmark_report.metric_summary()
    assert set(metrics) == {metric.value for metric in PromptBenchmarkMetric}
    assert all(item["checks"] > 0 for item in metrics.values())
    assert all(item["passed"] for item in metrics.values())
    assert all(item["failed_checks"] == 0 for item in metrics.values())


def test_json_report_is_parseable_and_content_light(benchmark_report):
    payload = benchmark_report.as_dict()
    assert payload["schema_version"] == PROMPT_BENCHMARK_SCHEMA_VERSION
    assert payload["passed"] is True
    assert payload["summary"] == {"cases": 9, "passed": 9, "failed": 0}
    assert set(payload["metrics"]) == {
        metric.value for metric in PromptBenchmarkMetric
    }
    assert set(payload["modes"]) == {
        mode.value for mode in PromptBenchmarkMode
    }

    encoded = json.dumps(payload, ensure_ascii=False)
    for prompt_body in (
        "Managed benchmark policy",
        "User benchmark preference",
        "Project benchmark policy",
        "Directory benchmark policy",
        "A second benchmark evidence item",
        "Preserve a recoverable compact benchmark scene",
        "def answer():",
        "请解释",
    ):
        assert prompt_body not in encoded


def test_one_failed_case_does_not_hide_other_results(monkeypatch):
    async def fail_main(_root: Path):
        raise RuntimeError("synthetic benchmark failure")

    monkeypatch.setattr(benchmark_prompt, "_run_main", fail_main)
    cases = tuple(
        case for case in BENCHMARK_CASES
        if case.mode in {PromptBenchmarkMode.MAIN, PromptBenchmarkMode.HEADLESS}
    )
    report = PromptBenchmarkRunner(cases=cases).run()
    results = {result.case_id: result for result in report.results}

    assert report.passed is False
    assert results["main"].passed is False
    assert "synthetic benchmark failure" in results["main"].error
    assert results["headless"].passed is True
    assert report.mode_summary()["headless"]["passed"] == 1


def test_cli_lists_without_sdk_and_runs_one_selected_case(capsys):
    rc = main(["--list", "--json", "--no-sdk"])
    listed = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(listed) == 8
    assert "sdk" not in {item["mode"] for item in listed}

    rc = main(["--case", "compact", "--no-sdk", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["passed"] is True
    assert [item["case_id"] for item in report["results"]] == ["compact"]
