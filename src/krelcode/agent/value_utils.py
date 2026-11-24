"""Small typed value helpers shared by Prompt and Context adapters."""

from __future__ import annotations

import json
from typing import Callable, Iterable, TypeVar

_T = TypeVar("_T")


def value_or(value: _T | None, fallback: _T) -> _T:
    return fallback if value is None else value


def merge_by_key(
    older: Iterable[_T],
    newer: Iterable[_T],
    key: Callable[[_T], str],
    *,
    limit: int,
) -> tuple[_T, ...]:
    merged = {key(item): item for item in older}
    for item in newer:
        merged[key(item)] = item
    return tuple(list(merged.values())[-max(1, limit):])


def short_json(value: object, limit: int = 200) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raw = repr(value)
    return raw if len(raw) <= limit else raw[:limit] + "…"


def nonnegative_int(value: object) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def nonnegative_float(value: object) -> float:
    if not isinstance(value, (int, float, str, bytes)):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError, OverflowError):
        return 0.0


__all__ = [
    "merge_by_key",
    "nonnegative_float",
    "nonnegative_int",
    "short_json",
    "value_or",
]
