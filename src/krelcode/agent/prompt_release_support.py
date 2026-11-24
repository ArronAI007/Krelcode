"""Compatibility facade for :mod:`krelcode.agent.prompt.prompt_release_support`."""

from __future__ import annotations

from typing import Any

from .prompt import prompt_release_support as _impl
from .prompt.prompt_release_support import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
