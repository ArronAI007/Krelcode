"""Compatibility facade for :mod:`krelcode.agent.context.tool_microcompact`."""

from __future__ import annotations

from typing import Any

from .context import tool_microcompact as _impl
from .context.tool_microcompact import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
