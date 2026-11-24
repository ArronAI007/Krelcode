"""Render restored Memory entries through the Evidence channel."""

from __future__ import annotations

from typing import Sequence

from .evidence import evidence_record_from_memory, render_evidence_set


def render_memory_context(entries: Sequence[object]) -> str:
    if not entries:
        return ""
    records = tuple(evidence_record_from_memory(entry) for entry in entries)
    return render_evidence_set(records, origin="restored_memory")


__all__ = ["render_memory_context"]
