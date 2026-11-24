"""Compatibility facade for shared core token estimators."""

from __future__ import annotations

from typing import Any

from ..core import tokens as _impl
from ..core.tokens import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
