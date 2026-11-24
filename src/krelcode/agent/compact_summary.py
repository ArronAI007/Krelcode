"""Compatibility facade for :mod:`krelcode.agent.context.compact_summary`."""

from __future__ import annotations

from typing import Any

from .context import compact_summary as _impl
from .context.compact_summary import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
