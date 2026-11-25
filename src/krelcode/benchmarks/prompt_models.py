"""Provider-neutral DTOs for deterministic Prompt/Context benchmarks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from ..agent.context.context_cache_ledger import ContextCacheLedger
from ..agent.prompt.prompt_plan import PromptPlanSnapshot


PROMPT_BENCHMARK_SCHEMA_VERSION = 1


class PromptBenchmarkMode(str, Enum):
    """The nine execution surfaces required by the benchmark contract."""

    MAIN = "main"
    HEADLESS = "headless"
    STREAM_JSON = "stream-json"
    SDK = "sdk"
    SUBAGENT = "subagent"
    RESUME = "resume"
    FORK = "fork"
    REWIND = "rewind"
    COMPACT = "compact"


class PromptBenchmarkMetric(str, Enum):
    """Stable machine-readable acceptance metric identifiers."""

    RELEVANT_SCOPE_ONLY = "relevant_scope_only"
    MEMORY_NOT_INSTRUCTION = "memory_not_instruction"
    RULE_PRECEDENCE_STABLE = "rule_precedence_stable"
    SUBAGENT_HISTORY_ISOLATED = "subagent_history_isolated"
    SUBAGENT_MEMORY_EVIDENCE = "subagent_memory_evidence"
    STABLE_PREFIX_ISOLATED = "stable_prefix_isolated"
    COMPACT_RECOVERABLE = "compact_recoverable"
    PROVIDER_HASH_EXACT = "provider_hash_exact"


@dataclass(frozen=True)
class PromptBenchmarkCase:
    id: str
    mode: PromptBenchmarkMode
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "mode": self.mode.value,
            "description": self.description,
        }


@dataclass(frozen=True)
class PromptBenchmarkCheck:
    metric: PromptBenchmarkMetric
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric.value,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PromptBenchmarkResult:
    case_id: str
    mode: PromptBenchmarkMode
    passed: bool
    checks: tuple[PromptBenchmarkCheck, ...] = ()
    prompt_plan: Mapping[str, object] = field(default_factory=dict)
    ledger: Mapping[str, object] = field(default_factory=dict)
    facts: Mapping[str, object] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "mode": self.mode.value,
            "passed": self.passed,
            "checks": [item.as_dict() for item in self.checks],
            "prompt_plan": dict(self.prompt_plan),
            "ledger": dict(self.ledger),
            "facts": dict(self.facts),
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class PromptBenchmarkReport:
    schema_version: int
    generated_at: str
    passed: bool
    results: tuple[PromptBenchmarkResult, ...]
    duration_ms: int

    def metric_summary(self) -> dict[str, dict[str, object]]:
        summary: dict[str, dict[str, object]] = {}
        for metric in PromptBenchmarkMetric:
            checks = [
                check
                for result in self.results
                for check in result.checks
                if check.metric is metric
            ]
            passed = sum(1 for check in checks if check.passed)
            summary[metric.value] = {
                "passed": bool(checks) and passed == len(checks),
                "checks": len(checks),
                "passed_checks": passed,
                "failed_checks": len(checks) - passed,
            }
        return summary

    def mode_summary(self) -> dict[str, dict[str, object]]:
        summary: dict[str, dict[str, object]] = {}
        for mode in PromptBenchmarkMode:
            results = [item for item in self.results if item.mode is mode]
            summary[mode.value] = {
                "covered": bool(results),
                "cases": len(results),
                "passed": sum(1 for item in results if item.passed),
                "failed": sum(1 for item in results if not item.passed),
            }
        return summary

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "summary": {
                "cases": len(self.results),
                "passed": sum(1 for item in self.results if item.passed),
                "failed": sum(1 for item in self.results if not item.passed),
            },
            "metrics": self.metric_summary(),
            "modes": self.mode_summary(),
            "results": [item.as_dict() for item in self.results],
        }


@dataclass
class _ScenarioOutcome:
    checks: list[PromptBenchmarkCheck]
    prompt_plan: PromptPlanSnapshot | None = None
    ledger: ContextCacheLedger | None = None
    facts: dict[str, object] = field(default_factory=dict)
