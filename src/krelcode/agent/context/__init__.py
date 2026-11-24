"""Context domain: workspace snapshots, assembly, rules and checkpoints."""

from __future__ import annotations

from typing import Any

from . import workspace as _workspace
from .workspace import *  # noqa: F401,F403


def __getattr__(name: str) -> Any:
    return getattr(_workspace, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_workspace)))
