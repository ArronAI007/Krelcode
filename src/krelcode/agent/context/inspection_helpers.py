"""Small formatting and provenance helpers for Context inspection."""

from __future__ import annotations

from typing import Sequence, TypeVar

from krelcode.agent.context.context_contracts import ContextLayer
from krelcode.agent.prompt.prompt_plan import PromptFragmentSnapshot

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


_T = TypeVar("_T")


def _bounded(items: Sequence[_T], limit: int) -> tuple[list[_T], int]:
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
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _layer_memory_metadata(layer: ContextLayer) -> tuple[tuple[str, ...], dict[str, int], int]:
    metadata = layer.metadata or {}
    raw_ids = metadata.get("memory_entry_ids", ())
    ids = tuple(str(item) for item in raw_ids) if isinstance(
        raw_ids, (list, tuple, set)) else ()
    raw_scopes = metadata.get("memory_scope_counts", {})
    scopes = {
        str(key): _int_value(value)
        for key, value in raw_scopes.items()
    } if isinstance(raw_scopes, dict) else {}
    tokens = _int_value(metadata.get("memory_tokens", 0))
    return ids, scopes, tokens

