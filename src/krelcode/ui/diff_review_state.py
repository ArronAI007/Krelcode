"""Typed mutable state used while parsing a unified diff."""

from __future__ import annotations

from typing import NotRequired, TypedDict, cast


class CurrentDiff(TypedDict):
    hunks: list[object]
    notes: list[str]
    old_path: NotRequired[str]
    new_path: NotRequired[str]
    status: NotRequired[str]
    from_git: NotRequired[bool]
    rename: NotRequired[bool]
    binary: NotRequired[bool]
    hunk_header: NotRequired[str]


def empty_current_diff() -> CurrentDiff:
    """Return an empty parser state while keeping its full field contract."""
    return cast(CurrentDiff, {})


__all__ = ["CurrentDiff", "empty_current_diff"]
