"""Provider-neutral DTOs and content-light serialization for live benchmarks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from ..core.events import (
    ContextCompressed,
    EventBus,
    LLMResponseReceived,
    ToolCallCompleted,
    ToolCallStarted,
)
from ..core.messages import ToolCall
from ..session.memory import MemoryKind, MemoryScope


BEHAVIOR_BENCHMARK_SCHEMA_VERSION = 4


_IGNORED_WORKSPACE_PARTS = frozenset({
    ".git", ".krelcode", ".pytest_cache", "__pycache__",
})


class BehaviorMetric(str, Enum):
    """Machine-readable behavior acceptance metrics."""

    TASK_SUCCESS = "task_success"
    WORKFLOW_FOLLOWED = "workflow_followed"
    REQUIRED_TOOL_USE = "required_tool_use"
    TRAJECTORY_SCOPE = "trajectory_scope"
    TESTS_VERIFIED = "tests_verified"
    INSTRUCTION_PRECEDENCE_FOLLOWED = "instruction_precedence_followed"
    EVIDENCE_RESISTED = "evidence_resisted"
    WORKSPACE_PROTECTED = "workspace_protected"
    COMPACT_CONTINUITY = "compact_continuity"
    SAFE_EXECUTION = "safe_execution"
    TRIAL_STABILITY = "trial_stability"


class BehaviorErrorKind(str, Enum):
    """Stable failure classes for provider/runtime diagnostics."""

    TIMEOUT = "timeout"
    PROVIDER = "provider"
    SANDBOX = "sandbox"
    CONFIGURATION = "configuration"
    INTERNAL = "internal"


@dataclass(frozen=True)
class BehaviorCheck:
    metric: BehaviorMetric
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric.value,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MemorySeed:
    text: str
    scope: MemoryScope = MemoryScope.PROJECT
    kind: MemoryKind = MemoryKind.KNOWLEDGE
    source: str = "live-benchmark"
    pinned: bool = True


@dataclass(frozen=True)
class BehaviorCase:
    id: str
    description: str
    seed: Callable[[Path, Path], tuple[MemorySeed, ...]]
    execute: Callable[["BehaviorSession"], Awaitable["BehaviorOutcome"]]
    expected_workflow: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {"id": self.id, "description": self.description}
        if self.expected_workflow:
            payload["expected_workflow"] = self.expected_workflow
        return payload


@dataclass
class BehaviorOutcome:
    checks: list[BehaviorCheck]
    answer: str = ""
    facts: Mapping[str, object] = field(default_factory=dict)
    extra_tools: tuple[ToolObservation, ...] = ()
    extra_usage: Mapping[str, int] = field(default_factory=dict)
    extra_cost_usd: float | None = None


@dataclass(frozen=True)
class ToolObservation:
    call_id: str
    name: str
    arguments: Mapping[str, object]
    completed: bool
    is_error: bool
    data: Mapping[str, object]
    duration_ms: int

    @property
    def success(self) -> bool:
        return self.completed and not self.is_error

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "call_id": self.call_id,
            "name": self.name,
            "completed": self.completed,
            "is_error": self.is_error,
            "duration_ms": self.duration_ms,
        }
        path = self.arguments.get("path")
        if path is not None:
            result["path"] = str(path)
        exit_code = self.data.get("exit_code")
        if exit_code is not None:
            result["exit_code"] = exit_code
        if include_content:
            result["arguments"] = dict(self.arguments)
        return result


@dataclass
class _LiveToolCall:
    call: ToolCall
    started_at: float
    completed: bool = False
    is_error: bool = False
    data: Mapping[str, object] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started_at) * 1000))


class BehaviorEventObserver:
    """Collect a bounded tool trajectory and content-free runtime signals."""

    def __init__(self) -> None:
        self._calls: list[_LiveToolCall] = []
        self.llm_calls = 0
        self.compaction_events = 0

    def attach(self, bus: EventBus) -> Callable[[], None]:
        return bus.subscribe(self.on_event)

    def on_event(self, event) -> None:
        if isinstance(event, ToolCallStarted):
            self._calls.append(_LiveToolCall(
                call=event.tool_call, started_at=time.monotonic()))
        elif isinstance(event, ToolCallCompleted):
            target = next((
                item for item in reversed(self._calls)
                if not item.completed
                and item.call.id == event.tool_call.id
                and item.call.name == event.tool_call.name
            ), None)
            if target is not None:
                target.completed = True
                target.is_error = bool(event.is_error)
                target.data = dict(event.data)
        elif isinstance(event, LLMResponseReceived):
            self.llm_calls += 1
        elif isinstance(event, ContextCompressed):
            self.compaction_events += 1

    @property
    def tools(self) -> tuple[ToolObservation, ...]:
        return tuple(ToolObservation(
            call_id=item.call.id,
            name=item.call.name,
            arguments=dict(item.call.arguments),
            completed=item.completed,
            is_error=item.is_error,
            data=dict(item.data),
            duration_ms=item.duration_ms,
        ) for item in self._calls)

    @property
    def tool_count(self) -> int:
        return len(self._calls)


@dataclass(frozen=True)
class WorkspaceDiff:
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.created, *self.modified, *self.deleted}))

    @property
    def empty(self) -> bool:
        return not self.paths

    def as_dict(self) -> dict[str, object]:
        return {
            "created": list(self.created),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "changed_count": len(self.paths),
        }


@dataclass(frozen=True)
class BehaviorTrialResult:
    case_id: str
    trial: int
    passed: bool
    checks: tuple[BehaviorCheck, ...]
    answer: str = ""
    tools: tuple[ToolObservation, ...] = ()
    usage: Mapping[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    duration_ms: int = 0
    error: str = ""
    error_kind: str = ""
    workspace_diff: WorkspaceDiff = WorkspaceDiff()
    facts: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "case_id": self.case_id,
            "trial": self.trial,
            "passed": self.passed,
            "checks": [item.as_dict() for item in self.checks],
            "tools": [
                item.as_dict(include_content=include_content)
                for item in self.tools
            ],
            "tool_count": len(self.tools),
            "usage": dict(self.usage),
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "error_kind": self.error_kind,
            "workspace_diff": self.workspace_diff.as_dict(),
            "facts": dict(self.facts),
        }
        if include_content:
            payload["answer"] = self.answer
        return payload


@dataclass(frozen=True)
class BehaviorReport:
    schema_version: int
    generated_at: str
    provider: str
    model: str
    passed: bool
    results: tuple[BehaviorTrialResult, ...]
    duration_ms: int
    profile: Mapping[str, object] = field(default_factory=dict)

    def case_summary(self) -> dict[str, dict[str, object]]:
        summary: dict[str, dict[str, object]] = {}
        case_ids = tuple(dict.fromkeys(item.case_id for item in self.results))
        for case_id in case_ids:
            trials = [item for item in self.results if item.case_id == case_id]
            passed = sum(1 for item in trials if item.passed)
            costs = [
                float(item.cost_usd) for item in trials
                if item.cost_usd is not None
            ]
            durations = [max(0, int(item.duration_ms)) for item in trials]
            pass_rate = (passed / len(trials)) if trials else 0.0
            confidence_low, confidence_high = _wilson_interval(
                passed, len(trials))
            summary[case_id] = {
                "trials": len(trials),
                "passed": passed,
                "failed": len(trials) - passed,
                "pass_rate": pass_rate,
                "pass_rate_ci_95": [confidence_low, confidence_high],
                "pass_at_k": passed > 0,
                "pass_hat_k": bool(trials) and passed == len(trials),
                "stable": bool(trials) and passed == len(trials),
                "flaky": 0 < passed < len(trials),
                "tools": sum(len(item.tools) for item in trials),
                "tool_errors": sum(
                    1 for item in trials for tool in item.tools
                    if not tool.success),
                "duration_ms": sum(item.duration_ms for item in trials),
                "p50_duration_ms": _percentile(durations, 50),
                "p95_duration_ms": _percentile(durations, 95),
                "input_tokens": sum(
                    int(item.usage.get("input_tokens", 0) or 0)
                    for item in trials),
                "output_tokens": sum(
                    int(item.usage.get("output_tokens", 0) or 0)
                    for item in trials),
                "cost_usd": round(sum(costs), 6) if costs else None,
            }
        return summary

    def metric_summary(self) -> dict[str, dict[str, object]]:
        case_summary = self.case_summary()
        summary: dict[str, dict[str, object]] = {}
        for metric in BehaviorMetric:
            if metric is BehaviorMetric.TRIAL_STABILITY:
                checks = len(case_summary)
                passed = sum(
                    1 for value in case_summary.values()
                    if value["stable"])
            else:
                selected = [
                    check for result in self.results for check in result.checks
                    if check.metric is metric
                ]
                checks = len(selected)
                passed = sum(1 for check in selected if check.passed)
            summary[metric.value] = {
                "covered": bool(checks),
                "passed": bool(checks) and passed == checks,
                "checks": checks,
                "passed_checks": passed,
                "failed_checks": checks - passed,
            }
        return summary

    def error_summary(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for result in self.results:
            if not result.error_kind:
                continue
            summary[result.error_kind] = summary.get(result.error_kind, 0) + 1
        return summary

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "BehaviorReport":
        """Restore a strict content-light report for shard aggregation."""
        return _behavior_report_from_mapping(payload)

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        cases = self.case_summary()
        costs = [
            float(item.cost_usd) for item in self.results
            if item.cost_usd is not None
        ]
        durations = [max(0, int(item.duration_ms)) for item in self.results]
        passed_trials = sum(1 for item in self.results if item.passed)
        failed_trials = len(self.results) - passed_trials
        tool_calls = sum(len(item.tools) for item in self.results)
        tool_failures = sum(
            1 for item in self.results for tool in item.tools
            if not tool.success)
        ci_low, ci_high = _wilson_interval(passed_trials, len(self.results))
        return {
            "schema_version": self.schema_version,
            "kind": "live_model_behavior",
            "content_light": not include_content,
            "generated_at": self.generated_at,
            "provider": self.provider,
            "model": self.model,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "profile": dict(self.profile),
            "summary": {
                "cases": len(cases),
                "stable_cases": sum(
                    1 for item in cases.values() if item["stable"]),
                "trials": len(self.results),
                "passed_trials": passed_trials,
                "failed_trials": failed_trials,
                "trial_success_rate": (
                    passed_trials / len(self.results) if self.results else 0.0),
                "trial_success_rate_ci_95": [ci_low, ci_high],
                "pass_at_k": sum(
                    1 for item in cases.values() if item["pass_at_k"]),
                "pass_hat_k": sum(
                    1 for item in cases.values() if item["pass_hat_k"]),
                "flaky_cases": sum(
                    1 for item in cases.values() if item["flaky"]),
                "pass_rate": (
                    passed_trials / len(self.results) if self.results else 0.0
                ),
                "tool_calls": tool_calls,
                "tool_failures": tool_failures,
                "tool_error_rate": (
                    tool_failures / tool_calls if tool_calls else 0.0),
                "p50_trial_duration_ms": _percentile(durations, 50),
                "p95_trial_duration_ms": _percentile(durations, 95),
                "input_tokens": sum(
                    int(item.usage.get("input_tokens", 0) or 0)
                    for item in self.results),
                "output_tokens": sum(
                    int(item.usage.get("output_tokens", 0) or 0)
                    for item in self.results),
                "total_tokens": sum(
                    int(item.usage.get("total_tokens", 0) or 0)
                    for item in self.results),
                "cost_usd": round(sum(costs), 6) if costs else None,
                "errors": self.error_summary(),
            },
            "cases": cases,
            "metrics": self.metric_summary(),
            "results": [
                item.as_dict(include_content=include_content)
                for item in self.results
            ],
        }


def _percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(max(0, int(value)) for value in values)
    rank = max(0, min(len(ordered) - 1,
                      int(round((percentile / 100.0) * (len(ordered) - 1)))))
    return ordered[rank]


def _wilson_interval(successes: int, total: int) -> tuple[float, float]:
    """95% Wilson score interval for a bounded pass-rate estimate."""
    if total <= 0:
        return 0.0, 0.0
    z = 1.96
    ratio = max(0.0, min(1.0, successes / total))
    denominator = 1.0 + z * z / total
    center = (ratio + z * z / (2.0 * total)) / denominator
    margin = z * (
        (ratio * (1.0 - ratio) / total + z * z / (4.0 * total * total)) ** 0.5
    ) / denominator
    return round(max(0.0, center - margin), 6), round(
        min(1.0, center + margin), 6)


def _report_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} 必须是 object")
    return value


def _report_int(value: object, label: str) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是整数") from exc


def _report_sequence(value: object, label: str) -> Sequence[object]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} 必须是数组")
    return value


def _behavior_report_from_mapping(payload: Mapping[str, object]) -> BehaviorReport:
    """Strictly restore a saved content-light report.

    Saved shards are enough to reconstruct every aggregate metric because the
    content-light contract retains checks, tool outcomes and trial durations.
    Free-form answers and tool arguments are rejected rather than ignored.
    """
    if payload.get("kind") != "live_model_behavior":
        raise ValueError("不是 live_model_behavior 报告")
    if int(payload.get("schema_version", 0) or 0) != BEHAVIOR_BENCHMARK_SCHEMA_VERSION:
        raise ValueError("live benchmark report schema 不兼容")
    profile = dict(_report_mapping(payload.get("profile", {}), "profile"))
    results: list[BehaviorTrialResult] = []
    for index, raw_result in enumerate(
        _report_sequence(payload.get("results"), "results")
    ):
        item = _report_mapping(raw_result, f"results[{index}]")
        if "answer" in item:
            raise ValueError("分片聚合只接受 content-light report（禁止 answer）")
        checks: list[BehaviorCheck] = []
        for raw_check in _report_sequence(item.get("checks"), "checks"):
            check = _report_mapping(raw_check, "check")
            try:
                metric = BehaviorMetric(str(check.get("metric", "")))
            except ValueError as exc:
                raise ValueError(
                    f"未知 benchmark metric: {check.get('metric')}") from exc
            checks.append(BehaviorCheck(
                metric=metric,
                passed=bool(check.get("passed", False)),
                detail=str(check.get("detail", "") or ""),
            ))
        tools: list[ToolObservation] = []
        for raw_tool in _report_sequence(item.get("tools"), "tools"):
            tool = _report_mapping(raw_tool, "tool")
            if "arguments" in tool:
                raise ValueError(
                    "分片聚合只接受 content-light report（禁止工具 arguments）")
            arguments: dict[str, object] = {}
            if tool.get("path") is not None:
                arguments["path"] = str(tool["path"])
            data: dict[str, object] = {}
            if tool.get("exit_code") is not None:
                data["exit_code"] = tool["exit_code"]
            tools.append(ToolObservation(
                call_id=str(tool.get("call_id", "") or ""),
                name=str(tool.get("name", "") or ""),
                arguments=arguments,
                completed=bool(tool.get("completed", False)),
                is_error=bool(tool.get("is_error", False)),
                data=data,
                duration_ms=max(0, _report_int(
                    tool.get("duration_ms"), "tool.duration_ms")),
            ))
        diff = _report_mapping(
            item.get("workspace_diff", {}), "workspace_diff")
        usage_raw = _report_mapping(item.get("usage", {}), "usage")
        facts = dict(_report_mapping(item.get("facts", {}), "facts"))
        cost = item.get("cost_usd")
        results.append(BehaviorTrialResult(
            case_id=str(item.get("case_id", "") or ""),
            trial=max(1, _report_int(item.get("trial"), "trial")),
            passed=bool(item.get("passed", False)),
            checks=tuple(checks),
            tools=tuple(tools),
            usage={
                str(key): _report_int(value, f"usage.{key}")
                for key, value in usage_raw.items()
            },
            cost_usd=None if cost is None else float(cost),
            duration_ms=max(0, _report_int(
                item.get("duration_ms"), "duration_ms")),
            error=str(item.get("error", "") or ""),
            error_kind=str(item.get("error_kind", "") or ""),
            workspace_diff=WorkspaceDiff(
                created=tuple(str(value) for value in _report_sequence(
                    diff.get("created"), "workspace_diff.created")),
                modified=tuple(str(value) for value in _report_sequence(
                    diff.get("modified"), "workspace_diff.modified")),
                deleted=tuple(str(value) for value in _report_sequence(
                    diff.get("deleted"), "workspace_diff.deleted")),
            ),
            facts=facts,
        ))
    if any(not result.case_id for result in results):
        raise ValueError("benchmark result 缺少 case_id")
    if len({(item.case_id, item.trial) for item in results}) != len(results):
        raise ValueError("benchmark report 包含重复 case/trial")
    return BehaviorReport(
        schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at=str(payload.get("generated_at", "") or ""),
        provider=str(payload.get("provider", "") or ""),
        model=str(payload.get("model", "") or ""),
        passed=bool(payload.get("passed", False)),
        results=tuple(results),
        duration_ms=max(0, _report_int(
            payload.get("duration_ms"), "duration_ms")),
        profile=profile,
    )
