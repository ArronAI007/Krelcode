"""Content-light live benchmark report summaries used by production commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_BEHAVIOR_REPORT_PATH = ".krelcode/prompts/benchmarks/latest.json"
BEHAVIOR_REPORT_KIND = "live_model_behavior"
BEHAVIOR_REPORT_STORE_VERSION = 1


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: object) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _float(value: object) -> float | None:
    if not isinstance(value, (int, float, str, bytes)):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class BehaviorReportSummary:
    """Bounded, content-light view of a saved live behaviour report."""

    path: str
    schema_version: int
    generated_at: str
    provider: str
    model: str
    passed: bool
    duration_ms: int
    cases: int
    stable_cases: int
    trials: int
    passed_trials: int
    failed_trials: int
    pass_at_k: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None
    error_counts: Mapping[str, int]
    profile: Mapping[str, object]

    @property
    def pass_rate(self) -> float:
        return self.passed_trials / self.trials if self.trials else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "provider": self.provider,
            "model": self.model,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "cases": self.cases,
            "stable_cases": self.stable_cases,
            "trials": self.trials,
            "passed_trials": self.passed_trials,
            "failed_trials": self.failed_trials,
            "pass_at_k": self.pass_at_k,
            "pass_rate": self.pass_rate,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "error_counts": dict(self.error_counts),
            "profile": dict(self.profile),
            "content_light": True,
        }


def load_behavior_report_summary(path: Path | str) -> BehaviorReportSummary:
    """Load a saved report and expose only bounded aggregate metadata."""
    source = Path(path).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取 benchmark report: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("benchmark report 不是合法 JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("benchmark report 顶层必须是对象")
    if raw.get("kind") != BEHAVIOR_REPORT_KIND:
        raise ValueError("不是 live_model_behavior 报告")
    summary = _mapping(raw.get("summary"))
    errors = _mapping(summary.get("errors"))
    profile = _mapping(raw.get("profile"))
    return BehaviorReportSummary(
        path=str(source),
        schema_version=_integer(raw.get("schema_version")),
        generated_at=str(raw.get("generated_at", "") or ""),
        provider=str(raw.get("provider", "") or ""),
        model=str(raw.get("model", "") or ""),
        passed=bool(raw.get("passed", False)),
        duration_ms=_integer(raw.get("duration_ms")),
        cases=_integer(summary.get("cases")),
        stable_cases=_integer(summary.get("stable_cases")),
        trials=_integer(summary.get("trials")),
        passed_trials=_integer(summary.get("passed_trials")),
        failed_trials=_integer(summary.get("failed_trials")),
        pass_at_k=_integer(summary.get("pass_at_k")),
        tool_calls=_integer(summary.get("tool_calls")),
        input_tokens=_integer(summary.get("input_tokens")),
        output_tokens=_integer(summary.get("output_tokens")),
        total_tokens=_integer(summary.get("total_tokens")),
        cost_usd=_float(summary.get("cost_usd")),
        error_counts={
            str(key): _integer(value)
            for key, value in sorted(errors.items())
            if _integer(value)
        },
        profile={
            str(key): value
            for key, value in profile.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        },
    )


__all__ = [
    "BEHAVIOR_REPORT_KIND",
    "BEHAVIOR_REPORT_STORE_VERSION",
    "BehaviorReportSummary",
    "DEFAULT_BEHAVIOR_REPORT_PATH",
    "load_behavior_report_summary",
]
