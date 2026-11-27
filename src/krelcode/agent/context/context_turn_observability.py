"""Content-light helpers for context-turn observability."""

from __future__ import annotations

from krelcode.agent.context.context_source import ContextAssembly
from krelcode.agent.turn.runtime_ports import ContextTurnHostPort


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


def knowledge_selection(
    assembly: ContextAssembly,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, int], ...],
    int,
]:
    """Extract knowledge document/chunk/source identities and token usage."""
    for layer in assembly.layers:
        metadata = layer.metadata or {}
        raw_documents = metadata.get("knowledge_document_ids", ())
        raw_chunks = metadata.get("knowledge_hit_ids", ())
        raw_sources = metadata.get("knowledge_sources", ())
        documents = tuple(str(item) for item in raw_documents) if isinstance(
            raw_documents, (list, tuple, set)) else ()
        chunks = tuple(str(item) for item in raw_chunks) if isinstance(
            raw_chunks, (list, tuple, set)) else ()
        sources = tuple(str(item) for item in raw_sources) if isinstance(
            raw_sources, (list, tuple, set)) else ()
        scope_counts: dict[str, int] = {}
        raw_hits = metadata.get("knowledge_hits", ())
        if isinstance(raw_hits, (list, tuple)):
            for hit in raw_hits:
                if not isinstance(hit, dict):
                    continue
                scope = str(hit.get("scope") or "")
                if scope:
                    scope_counts[scope] = scope_counts.get(scope, 0) + 1
        tokens = _non_negative_int(metadata.get("knowledge_tokens", 0))
        if documents or chunks or sources or tokens:
            return (
                documents,
                chunks,
                sources,
                tuple(sorted(scope_counts.items())),
                tokens,
            )
    return (), (), (), (), 0


__all__ = [
    "dropped_context_items",
    "knowledge_selection",
    "memory_selection",
]
