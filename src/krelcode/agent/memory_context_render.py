"""Compatibility facade for :mod:`krelcode.agent.context.memory_context_render`."""

from __future__ import annotations

from typing import Any

from .context import memory_context_render as _impl
from .context.memory_context_render import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
