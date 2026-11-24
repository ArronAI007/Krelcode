"""Narrow host and persistence ports used by context checkpoint recovery."""

from __future__ import annotations

from typing import Mapping, Optional, Protocol, Sequence

from krelcode.core.events import EventBus
from krelcode.core.messages import Message
from krelcode.contracts.tool import ToolContext


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
    @property
    def id(self) -> str: ...


class CompactStorePort(Protocol):
    def load(self, checkpoint_id: str) -> object: ...


class MemoryStorePort(Protocol):
    def search(self, query: str, *, limit: int = 8) -> Sequence[MemoryEntryPort]: ...


class WorkingStorePort(Protocol):
    def restore_snapshot(
        self, raw: Mapping[str, object]
    ) -> WorkingContextPort: ...
    def snapshot(self) -> WorkingContextPort: ...
    def invalidate_files(
        self, paths: Sequence[str], *, reason: str = "",
    ) -> object: ...


class ContextManagerRestorePort(Protocol):
    @property
    def last_summary(self) -> object: ...

    @property
    def last_summary_source(self) -> str: ...

    @property
    def last_generation(self) -> int: ...

    def rebuild_state(self, history: Sequence[Message]) -> object: ...


class CheckpointLoopPort(Protocol):
    @property
    def history(self) -> list[Message]: ...

    @property
    def tool_context(self) -> ToolContext: ...

    @property
    def context_manager(self) -> Optional[ContextManagerRestorePort]: ...

    @property
    def working_context_store(self) -> WorkingStorePort: ...

    @property
    def last_compact_checkpoint(self) -> object: ...

    @property
    def last_context_restore(self) -> Optional[object]: ...

    @property
    def last_context_plan(self) -> Optional[ContextPlanPort]: ...

    @property
    def last_context_cache_ledger(self) -> object: ...

    @property
    def previous_context_cache_ledger(self) -> object: ...

    @property
    def pending_context_refresh(self) -> bool: ...

    @property
    def restored_memory_context(self) -> tuple[str, ...]: ...

    @property
    def restored_relevant_memory_ids(self) -> tuple[str, ...]: ...

    @property
    def restored_context_checkpoint_id(self) -> str: ...

    @property
    def context_integrity(self) -> object: ...

    @property
    def context_workspace_fingerprint(self) -> object: ...

    @property
    def restored_workspace_fingerprint(self) -> object: ...

    @property
    def bus(self) -> EventBus: ...

    def rebuild_working_context(self) -> WorkingContextPort: ...
    async def refresh_workspace_context(self) -> object: ...
    def update_runtime_state(self, **values: object) -> None: ...


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
