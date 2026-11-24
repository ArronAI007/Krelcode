"""Compatibility facade for :mod:`krelcode.agent.turn.loop`."""

from __future__ import annotations

from typing import Any

from .turn import loop as _impl
from .turn.loop import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
