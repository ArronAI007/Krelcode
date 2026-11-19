"""Offline structure and live model behavior benchmarks for Krelcode."""

from __future__ import annotations

__all__ = [
    "BEHAVIOR_BENCHMARK_SCHEMA_VERSION",
    "BEHAVIOR_CASES",
    "BENCHMARK_CASES",
    "PROMPT_BENCHMARK_SCHEMA_VERSION",
    "BehaviorBenchmarkRunner",
    "BehaviorCase",
    "BehaviorCheck",
    "BehaviorMetric",
    "BehaviorReport",
    "BehaviorTrialResult",
    "PromptBenchmarkCase",
    "PromptBenchmarkCheck",
    "PromptBenchmarkMetric",
    "PromptBenchmarkMode",
    "PromptBenchmarkReport",
    "PromptBenchmarkResult",
    "PromptBenchmarkRunner",
    "main",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    if name.startswith("Behavior") or name.startswith("BEHAVIOR_"):
        from . import behavior

        return getattr(behavior, name)
    from . import prompt

    return getattr(prompt, name)
