"""Compatibility facade for :mod:`krelcode.agent.context.inspection_prompt`."""

from __future__ import annotations

from typing import Any

from .context import inspection_prompt as _impl
from .context.inspection_prompt import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
