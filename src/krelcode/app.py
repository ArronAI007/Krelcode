"""Backward-compatible application facade."""

from __future__ import annotations

from .bootstrap.composition import create_provider, resolve_settings
from .bootstrap.composition import *  # noqa: F401,F403
from .bootstrap.main import AsyncMain, async_main
from .entrypoints.main import main
