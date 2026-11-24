"""Content-light helpers for context-turn observability."""

from __future__ import annotations

from krelcode.agent.context.context_source import ContextAssembly
from krelcode.agent.runtime_ports import ContextTurnHostPort


def dropped_context_items(host: ContextTurnHostPort) -> tuple[str, ...]:
    """Return stable workspace omission identifiers for ContextDropped."""
    workspace = host.workspace_context
    items = (
        f"workspace:{item}" for item in (workspace.omitted if workspace else ())
    )
    return tuple(dict.fromkeys(items))


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def memory_selection(
    assembly: ContextAssembly,
) -> tuple[tuple[str, ...], dict[str, int], int]:
    """Extract memory IDs, scope counts and tokens from assembly metadata."""
    for layer in assembly.layers:
        metadata = layer.metadata or {}
        raw_ids = metadata.get("memory_entry_ids", ())
        ids = tuple(str(item) for item in raw_ids) if isinstance(
            raw_ids, (list, tuple, set)) else ()
        raw_scopes = metadata.get("memory_scope_counts", {})
        scopes = {
            str(key): _non_negative_int(value)
            for key, value in raw_scopes.items()
        } if isinstance(raw_scopes, dict) else {}
        tokens = _non_negative_int(metadata.get("memory_tokens", 0))
        if ids or scopes or tokens:
            return ids, scopes, tokens
    return (), {}, 0


__all__ = ["dropped_context_items", "memory_selection"]
