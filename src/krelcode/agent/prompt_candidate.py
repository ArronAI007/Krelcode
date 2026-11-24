"""Compatibility facade for :mod:`krelcode.agent.prompt.prompt_candidate`."""

from __future__ import annotations

from typing import Any

from .prompt import prompt_candidate as _impl
from .prompt.prompt_candidate import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
