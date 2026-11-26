"""Offline structure and live model behavior benchmarks for Krelcode."""

from __future__ import annotations

from typing import Any

__all__ = [
    "BEHAVIOR_BENCHMARK_SCHEMA_VERSION",
    "BEHAVIOR_CASES",
    "BENCHMARK_CASES",
    "BENCHMARK_SUITES",
    "BenchmarkSuite",
    "BenchmarkSuiteError",
    "FROZEN_PROMPT_PARITY_CONTRACT_HASH",
    "FROZEN_PROMPT_PARITY_SUITE_HASH",
    "PROMPT_BASELINE_SCHEMA_VERSION",
    "PROMPT_BENCHMARK_SCHEMA_VERSION",
    "PROMPT_PARITY_CONTRACT",
    "PROMPT_PARITY_CONTRACT_VERSION",
    "PROMPT_PARITY_MINIMUM_TRIALS",
    "PROMPT_PARITY_SCHEMA_VERSION",
    "PROMPT_TUNING_SCHEMA_VERSION",
    "AcceptanceArea",
    "AcceptanceCheckResult",
    "AcceptanceCheckStatus",
    "AcceptanceEvidenceKind",
    "AcceptanceRequirement",
    "FINAL_ACCEPTANCE_CONTRACT",
    "FINAL_ACCEPTANCE_CONTRACT_VERSION",
    "FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS",
    "FINAL_ACCEPTANCE_SCHEMA_VERSION",
    "FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH",
    "FinalAcceptanceContract",
    "FinalAcceptanceReport",
    "FinalAcceptanceRunner",
    "FinalAcceptanceStatus",
    "BehaviorBenchmarkRunner",
    "BehaviorCase",
    "BehaviorCheck",
    "BehaviorErrorKind",
    "BehaviorMetric",
    "BehaviorReport",
    "BehaviorReportSummary",
    "BehaviorTrialResult",
    "BehaviorFixture",
    "BehaviorFixturePort",
    "BenchmarkPorts",
    "EvaluationHarnessPort",
    "TraceReplayPort",
    "VersionManifestPort",
    "evaluate_and_admit",
    "evaluate_candidate",
    "DEFAULT_BEHAVIOR_REPORT_PATH",
    "aggregate_behavior_report_files",
    "aggregate_behavior_reports",
    "FailureAttribution",
    "FailureCause",
    "FailureTrajectoryAnalyzer",
    "ParityDimension",
    "ParityGateCheck",
    "ParityRequirement",
    "PromptBenchmarkCase",
    "PromptBenchmarkCheck",
    "PromptBenchmarkMetric",
    "PromptBenchmarkMode",
    "PromptBenchmarkReport",
    "PromptBenchmarkResult",
    "PromptBenchmarkRunner",
    "PromptParityBaseline",
    "PromptParityContract",
    "PromptParityGateResult",
    "PromptParityStatus",
    "PromptTuningCandidate",
    "PromptTuningReport",
    "TuningRisk",
    "ToolObservation",
    "analyze_failure_trajectories",
    "contract_summary",
    "evaluate_prompt_parity",
    "load_behavior_report",
    "load_prompt_baseline",
    "load_behavior_report_summary",
    "parse_shard",
    "resolve_suite",
    "run_offline_acceptance",
    "main",
    "save_prompt_baseline",
    "save_behavior_report",
    "shard_cases",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    if (
        name.startswith((
            "Acceptance",
            "FinalAcceptance",
            "FINAL_ACCEPTANCE",
            "FROZEN_FINAL_ACCEPTANCE",
        ))
        or name == "run_offline_acceptance"
    ):
        from . import acceptance

        return getattr(acceptance, name)
    if name in {
        "BehaviorFixture", "BehaviorFixturePort", "BenchmarkPorts",
        "EvaluationHarnessPort", "TraceReplayPort", "VersionManifestPort",
        "evaluate_and_admit", "evaluate_candidate",
    }:
        from . import ports

        return getattr(ports, name)
    if name in {
        "BehaviorReportSummary", "DEFAULT_BEHAVIOR_REPORT_PATH",
        "aggregate_behavior_report_files", "load_behavior_report",
        "load_behavior_report_summary", "save_behavior_report",
    }:
        from . import report_store

        return getattr(report_store, name)
    if name.startswith("BenchmarkSuite") or name in {
        "BENCHMARK_SUITES", "parse_shard", "resolve_suite", "shard_cases",
    }:
        from . import suite

        return getattr(suite, name)
    if (
        name.startswith("Behavior")
        or name.startswith("BEHAVIOR_")
        or name in {"ToolObservation", "aggregate_behavior_reports"}
    ):
        from . import behavior

        return getattr(behavior, name)
    if name.startswith(("Parity", "PromptParity", "PROMPT_PARITY", "PROMPT_BASELINE", "FROZEN_PROMPT")) or name in {
        "contract_summary", "evaluate_prompt_parity", "load_prompt_baseline",
        "save_prompt_baseline",
    }:
        from . import parity

        return getattr(parity, name)
    if (
        name.startswith(("Failure", "PromptTuning", "Tuning", "PROMPT_TUNING_"))
        or name == "analyze_failure_trajectories"
    ):
        from . import tuning

        return getattr(tuning, name)
    from . import prompt

    return getattr(prompt, name)
