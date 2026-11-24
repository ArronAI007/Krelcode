"""Small formatting and provenance helpers for Context inspection."""

from __future__ import annotations

from typing import Sequence

from .context_source import ContextLayer
from .prompt_plan import PromptFragmentSnapshot

def _short_hash(value: str) -> str:
    return (value or "(none)")[:16]


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _ids(items: Sequence[str], limit: int = 20) -> str:
    if not items:
        return "(none)"
    shown = ", ".join(items[:limit])
    return shown + (f" · … 另有 {len(items) - limit} 个"
                    if len(items) > limit else "")


def _bounded(items: Sequence[object], limit: int) -> tuple[list[object], int]:
    if limit <= 0 or len(items) <= limit:
        return list(items), 0
    return list(items[:limit]), len(items) - limit


def _join_provenance(value: object) -> str:
    if isinstance(value, (list, tuple)):
        normalized = [str(item) for item in value if str(item)]
        return ", ".join(normalized) if normalized else "(none)"
    text = str(value or "")
    return text or "(none)"


def _is_memory_fragment(fragment: PromptFragmentSnapshot) -> bool:
    return (
        fragment.id == "memory:scoped"
        or fragment.source in {
            "memory", "memory_retrieval", "inherited_memory",
        }
    )


def _int_value(value: object) -> int:
    """Read a bounded integer from a persisted ToolPromptSpec record."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _layer_memory_metadata(layer: ContextLayer) -> tuple[tuple[str, ...], dict[str, int], int]:
    metadata = layer.metadata or {}
    raw_ids = metadata.get("memory_entry_ids", ())
    ids = tuple(str(item) for item in raw_ids) if isinstance(
        raw_ids, (list, tuple, set)) else ()
    raw_scopes = metadata.get("memory_scope_counts", {})
    scopes = {
        str(key): max(0, int(value))
        for key, value in dict(raw_scopes).items()
    } if isinstance(raw_scopes, dict) else {}
    tokens = max(0, int(metadata.get("memory_tokens", 0) or 0))
    return ids, scopes, tokens

