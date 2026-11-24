"""Compatibility facade for :mod:`krelcode.agent.turn.dependencies`."""

from __future__ import annotations

from typing import Any

from .turn import dependencies as _impl
from .turn.dependencies import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
