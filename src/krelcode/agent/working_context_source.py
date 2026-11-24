"""Adapter exposing Working Context as volatile runtime context."""

from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, cast

from ..core.messages import Message

from .context_contracts import (
    CATEGORY_RUNTIME,
    ContextLayer,
    ContextTokenContribution,
    TRUST_UNTRUSTED,
)

class _WorkingEntryPort(Protocol):
    kind: str
    content: str


class _WorkingSnapshotPort(Protocol):
    text: str
    count: int
    entries: Sequence[_WorkingEntryPort]
    truncated: bool
    dropped_entries: int
    counts_by_kind: object
    sequence: int
    token_count: int


class _ContextRequestPort(Protocol):
    history: Sequence[Message]
    working_context: Optional[object]


class WorkingContextSource:
    """Project one immutable Working Context snapshot into ContextLayer DTOs.

    The adapter intentionally does not import the store module at runtime.  It
    only needs the snapshot protocol, which keeps the store independent from the
    context assembly stack and avoids an import cycle.
    """

    name = "working_context"

    def __init__(self, store: Optional[Any] = None) -> None:
        self.store = store
        self.last_snapshot: Optional[Any] = None

    def collect(self, request: object) -> Sequence[ContextLayer]:
        request = cast(_ContextRequestPort, request)
        snapshot = cast(Optional[_WorkingSnapshotPort], request.working_context)
        if snapshot is None and self.store is not None:
            snapshot = self.store.snapshot()
        if snapshot is None or not getattr(snapshot, "text", ""):
            self.last_snapshot = snapshot
            return ()
        latest_user = next(
            (message.content or "" for message in reversed(request.history)
             if message.role == "user"),
            "",
        ).strip()
        if (
            latest_user
            and snapshot.count == 1
            and snapshot.entries[0].kind == "goal"
            and snapshot.entries[0].content.strip() == latest_user
        ):
            # The current goal already exists verbatim in history. Injecting
            # Working Context before any tool fact exists only duplicates tokens.
            self.last_snapshot = snapshot
            return ()
        self.last_snapshot = snapshot
        return (ContextLayer(
            id="runtime:working-context",
            source=self.name,
            category=CATEGORY_RUNTIME,
            content=snapshot.text,
            trust=TRUST_UNTRUSTED,
            origin="working_context",
            priority=40,
            cacheable=False,
            compactable=True,
            metadata={
                "entries": snapshot.count,
                "truncated": snapshot.truncated,
                "dropped_entries": snapshot.dropped_entries,
                "kinds": snapshot.counts_by_kind,
                "sequence": snapshot.sequence,
            },
            token_contributions=(ContextTokenContribution(
                "runtime:working-context",
                CATEGORY_RUNTIME,
                snapshot.token_count,
            ),),
        ),)


__all__ = ["WorkingContextSource"]
