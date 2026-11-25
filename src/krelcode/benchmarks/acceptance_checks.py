"""Focused runtime checks used by final acceptance probes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Protocol

from .acceptance_models import (
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    AcceptanceRequirement,
)


class _ReportMetricsPort(Protocol):
    def metric_summary(self) -> dict[str, dict[str, object]]: ...

    def mode_summary(self) -> dict[str, dict[str, object]]: ...


def _check(
    requirement: AcceptanceRequirement,
    passed: bool,
    detail: str,
    evidence: Mapping[str, object] | None = None,
    *,
    pending: bool = False,
) -> AcceptanceCheckResult:
    status = (
        AcceptanceCheckStatus.PENDING if pending else
        AcceptanceCheckStatus.PASSED if passed else
        AcceptanceCheckStatus.FAILED
    )
    return AcceptanceCheckResult(
        id=requirement.id,
        area=requirement.area,
        status=status,
        evidence_kind=requirement.evidence_kind,
        required=True,
        detail=detail,
        evidence=evidence or {},
    )


def _safe_error(error: BaseException) -> str:
    """Return type-only diagnostics; exception text may contain user content."""
    return type(error).__name__


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_is_fresh(
    value: datetime | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    if value is None:
        return False
    age = (now - value).total_seconds()
    # Permit a small future clock skew but never an unbounded future timestamp.
    return -300.0 <= age <= max_age_seconds


def _report_metric(
    report: _ReportMetricsPort,
    metric_name: str,
) -> Mapping[str, object]:
    metrics = report.metric_summary()
    value = metrics.get(metric_name, {})
    return value if isinstance(value, Mapping) else {}


def _report_mode(
    report: _ReportMetricsPort,
    mode_name: str,
) -> Mapping[str, object]:
    modes = report.mode_summary()
    value = modes.get(mode_name, {})
    return value if isinstance(value, Mapping) else {}
