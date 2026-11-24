"""Narrow host and persistence ports used by context checkpoint recovery."""

from __future__ import annotations

from typing import Mapping, Optional, Protocol, Sequence

from ..core.events import EventBus
from ..core.messages import Message
from ..contracts.tool import ToolContext


class ContextPlanPort(Protocol):
    @property
    def full_hash(self) -> str: ...
    def as_dict(self) -> Mapping[str, object]: ...


class WorkingEntryPort(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def content(self) -> str: ...
    @property
    def metadata(self) -> Mapping[str, object]: ...


class WorkingContextPort(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def count(self) -> int: ...
    @property
    def entries(self) -> Sequence[WorkingEntryPort]: ...
    @property
    def truncated(self) -> bool: ...
    @property
    def dropped_entries(self) -> int: ...
    @property
    def counts_by_kind(self) -> Mapping[str, int]: ...
    @property
    def sequence(self) -> int: ...
    @property
    def token_count(self) -> int: ...

    def as_checkpoint_record(self) -> Mapping[str, object]: ...


class MemoryEntryPort(Protocol):
    id: str


class CompactStorePort(Protocol):
    def load(self, checkpoint_id: str) -> object: ...


class MemoryStorePort(Protocol):
    def search(self, query: str, *, limit: int = 8) -> Sequence[MemoryEntryPort]: ...


class WorkingStorePort(Protocol):
    def restore_snapshot(self, raw: object) -> WorkingContextPort: ...
    def snapshot(self) -> WorkingContextPort: ...
    def invalidate_files(
        self, paths: Sequence[str], *, reason: str = "",
    ) -> object: ...


class ContextManagerRestorePort(Protocol):
    last_summary: object
    last_summary_source: str
    last_generation: int

    def rebuild_state(self, history: Sequence[Message]) -> object: ...


class CheckpointLoopPort(Protocol):
    history: list[Message]
    tool_context: ToolContext
    context_manager: Optional[ContextManagerRestorePort]
    working_context_store: WorkingStorePort
    last_compact_checkpoint: object
    last_context_restore: Optional[object]
    last_context_plan: Optional[ContextPlanPort]
    last_context_cache_ledger: object
    previous_context_cache_ledger: object
    pending_context_refresh: bool
    restored_memory_context: tuple[str, ...]
    restored_relevant_memory_ids: tuple[str, ...]
    restored_context_checkpoint_id: str
    context_integrity: object
    context_workspace_fingerprint: object
    restored_workspace_fingerprint: object
    bus: EventBus

    def rebuild_working_context(self) -> WorkingContextPort: ...
    async def refresh_workspace_context(self) -> object: ...


__all__ = [
    "CheckpointLoopPort",
    "CompactStorePort",
    "ContextManagerRestorePort",
    "ContextPlanPort",
    "MemoryEntryPort",
    "MemoryStorePort",
    "WorkingStorePort",
    "WorkingContextPort",
    "WorkingEntryPort",
]
