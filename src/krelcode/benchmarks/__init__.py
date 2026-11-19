"""Content-light benchmarks for the Krelcode prompt/context pipeline."""

from __future__ import annotations

__all__ = [
    "BENCHMARK_CASES",
    "PROMPT_BENCHMARK_SCHEMA_VERSION",
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
    from . import prompt

    return getattr(prompt, name)
