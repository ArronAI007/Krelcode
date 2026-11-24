"""Auto-memory configuration resolution."""

from __future__ import annotations

import argparse
from typing import Any, Dict

from krelcode.session.memory_candidates import (
    DEFAULT_AUTO_THRESHOLD,
    DEFAULT_SUGGEST_THRESHOLD,
    AutoMemoryMode,
    coerce_auto_memory_mode,
)


def resolve_auto_memory_mode(
    args: argparse.Namespace,
    config: Dict[str, Any],
) -> AutoMemoryMode:
    """CLI > ``[memory].mode`` > conservative ``suggest`` default."""
    explicit = getattr(args, "memory_mode", None)
    if explicit:
        return coerce_auto_memory_mode(explicit)
    section = config.get("memory") or {}
    configured = section.get("mode") if isinstance(section, dict) else None
    return coerce_auto_memory_mode(configured or AutoMemoryMode.SUGGEST.value)


def resolve_auto_memory_thresholds(
    args: argparse.Namespace,
    config: Dict[str, Any],
) -> tuple[float, float]:
    section = config.get("memory") or {}
    if not isinstance(section, dict):
        section = {}
    suggest = getattr(args, "memory_suggest_threshold", None)
    auto = getattr(args, "memory_auto_threshold", None)
    try:
        suggest_value = float(
            suggest
            if suggest is not None
            else section.get("suggest_threshold", DEFAULT_SUGGEST_THRESHOLD)
        )
        auto_value = float(
            auto
            if auto is not None
            else section.get("auto_threshold", DEFAULT_AUTO_THRESHOLD)
        )
    except (TypeError, ValueError):
        suggest_value = DEFAULT_SUGGEST_THRESHOLD
        auto_value = DEFAULT_AUTO_THRESHOLD
    suggest_value = max(0.0, min(suggest_value, 1.0))
    auto_value = max(suggest_value, min(auto_value, 1.0))
    return suggest_value, auto_value
