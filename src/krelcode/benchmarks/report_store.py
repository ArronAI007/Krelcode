"""Content-light persistence for live model behavior reports.

The summary DTO and parser are production-owned in
:mod:`krelcode.agent.prompt_benchmark_report`; this benchmark module only adds
the executable report save/load/aggregate operations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from ..agent.prompt.prompt_benchmark_report import (
    BEHAVIOR_REPORT_KIND,
    BEHAVIOR_REPORT_STORE_VERSION,
    BehaviorReportSummary,
    DEFAULT_BEHAVIOR_REPORT_PATH,
    load_behavior_report_summary,
)

if TYPE_CHECKING:
    from .behavior_models import BehaviorReport


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def save_behavior_report(report: object, path: Path | str) -> Path:
    """Atomically save a live report with a strict content-light payload."""
    as_dict = getattr(report, "as_dict", None)
    if not callable(as_dict):
        raise TypeError("report 缺少 as_dict()")
    payload = as_dict(include_content=False)
    if not isinstance(payload, Mapping):
        raise ValueError("behavior report 必须序列化为对象")
    if payload.get("kind") != BEHAVIOR_REPORT_KIND:
        raise ValueError("不是 live_model_behavior 报告")
    results = tuple(
        item for item in payload.get("results", ())
        if isinstance(item, Mapping))
    if any("answer" in item for item in results):
        raise ValueError("content-light 报告不能包含 answer")
    if any(
        isinstance(tool, Mapping) and "arguments" in tool
        for result in results
        for tool in result.get("tools", ())
    ):
        raise ValueError("content-light 报告不能包含工具 arguments")

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        temporary.write_text(encoded + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def load_behavior_report(path: Path | str) -> "BehaviorReport":
    """Load one strict, content-light live report for aggregation."""
    source = Path(path).expanduser()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取 benchmark report: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"benchmark report 不是合法 JSON: {source}") from exc
    raw = _mapping(payload)
    if raw.get("kind") != BEHAVIOR_REPORT_KIND:
        raise ValueError(f"不是 live_model_behavior 报告: {source}")
    from .behavior_models import BehaviorReport

    try:
        return BehaviorReport.from_dict(raw)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def aggregate_behavior_report_files(
    paths: Sequence[Path | str],
    *,
    require_complete: bool = True,
) -> "BehaviorReport":
    """Merge deterministic shard reports without trusting summary counters."""
    from .behavior_reporting import aggregate_behavior_reports

    reports = [load_behavior_report(path) for path in paths]
    return aggregate_behavior_reports(
        reports, require_complete=require_complete)


__all__ = [
    "BEHAVIOR_REPORT_KIND",
    "BEHAVIOR_REPORT_STORE_VERSION",
    "BehaviorReportSummary",
    "DEFAULT_BEHAVIOR_REPORT_PATH",
    "aggregate_behavior_report_files",
    "load_behavior_report",
    "load_behavior_report_summary",
    "save_behavior_report",
]
