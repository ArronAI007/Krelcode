"""Defensive value readers for content-light Prompt release evidence."""

from __future__ import annotations

from typing import Mapping, Protocol


class BehaviorReportPort(Protocol):
    passed: bool
    profile: Mapping[str, object]
    schema_version: int
    provider: str
    model: str

    def case_summary(self) -> Mapping[str, Mapping[str, object]]: ...


def integer(value: object, default: int = 0) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def mapping_items(value: object, key: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Mapping):
        return ()
    items = value.get(key)
    if not isinstance(items, (list, tuple)):
        return ()
    return tuple(item for item in items if isinstance(item, Mapping))


def case_summary(report: object) -> Mapping[str, Mapping[str, object]]:
    method = getattr(report, "case_summary", None)
    if not callable(method):
        return {}
    raw = method()
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): value for key, value in raw.items()
        if isinstance(value, Mapping)
    }


__all__ = [
    "BehaviorReportPort",
    "case_summary",
    "integer",
    "mapping",
    "mapping_items",
]
