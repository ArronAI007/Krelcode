"""Compatibility facade for :mod:`krelcode.agent.turn.reminders`."""

from __future__ import annotations

from typing import Any

from .turn import reminders as _impl
from .turn.reminders import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
