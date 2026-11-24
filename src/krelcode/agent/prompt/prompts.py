"""Prompt profile facade backed by the compatibility-stable public module."""

from __future__ import annotations

from typing import Any

from .. import prompts as _impl
from ..prompts import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
