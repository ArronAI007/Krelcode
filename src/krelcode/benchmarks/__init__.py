"""Offline structure and live model behavior benchmarks for Krelcode."""

from __future__ import annotations

__all__ = [
    "BEHAVIOR_BENCHMARK_SCHEMA_VERSION",
    "BEHAVIOR_CASES",
    "BENCHMARK_CASES",
    "PROMPT_BENCHMARK_SCHEMA_VERSION",
    "PROMPT_TUNING_SCHEMA_VERSION",
    "BehaviorBenchmarkRunner",
    "BehaviorCase",
    "BehaviorCheck",
    "BehaviorErrorKind",
    "BehaviorMetric",
    "BehaviorReport",
    "BehaviorTrialResult",
    "FailureAttribution",
    "FailureCause",
    "FailureTrajectoryAnalyzer",
    "PromptBenchmarkCase",
    "PromptBenchmarkCheck",
    "PromptBenchmarkMetric",
    "PromptBenchmarkMode",
    "PromptBenchmarkReport",
    "PromptBenchmarkResult",
    "PromptBenchmarkRunner",
    "PromptTuningCandidate",
    "PromptTuningReport",
    "TuningRisk",
    "analyze_failure_trajectories",
    "main",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    if name.startswith("Behavior") or name.startswith("BEHAVIOR_"):
        from . import behavior

        return getattr(behavior, name)
    if (
        name.startswith(("Failure", "PromptTuning", "Tuning", "PROMPT_TUNING_"))
        or name == "analyze_failure_trajectories"
    ):
        from . import tuning

        return getattr(tuning, name)
    from . import prompt

    return getattr(prompt, name)
