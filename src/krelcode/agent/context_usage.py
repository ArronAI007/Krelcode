"""Compatibility facade for :mod:`krelcode.agent.context.context_usage`."""

from __future__ import annotations

from typing import Any

from .context import context_usage as _impl
from .context.context_usage import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
