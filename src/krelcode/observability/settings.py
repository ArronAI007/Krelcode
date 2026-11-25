"""Configuration for the local observability recorder."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ObservabilityOptions:
    enabled: bool = True
    queue_size: int = 4_096
    max_string_chars: int = 4_000
    max_collection_items: int = 100
    include_text_previews: bool = True

    @classmethod
    def from_args_and_config(
        cls,
        args: argparse.Namespace,
        config: Mapping[str, object],
    ) -> "ObservabilityOptions":
        section = config.get("observability")
        raw = section if isinstance(section, Mapping) else {}
        explicit = getattr(args, "observability", None)
        enabled = bool(raw.get("enabled", True)) if explicit is None else bool(explicit)
        return cls(
            enabled=enabled,
            queue_size=_bounded_int(raw.get("queue_size"), 4_096, 64, 65_536),
            max_string_chars=_bounded_int(
                raw.get("max_string_chars"), 4_000, 128, 100_000),
            max_collection_items=_bounded_int(
                raw.get("max_collection_items"), 100, 1, 1_000),
            include_text_previews=bool(
                raw.get("include_text_previews", True)),
        )


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    if not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


__all__ = ["ObservabilityOptions"]
