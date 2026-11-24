"""Typed collaboration boundary for the split memory-store mixins.

Runtime methods are implemented by ``memory_storage``, ``memory_views``,
``memory_context`` and ``memory_migration``.  This base declares the members
that cross those module boundaries so strict type checking does not depend on
MRO folklore.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ..core.redact import SecretRedactor
from .memory_models import (
    MemoryEntry,
    MemoryIndexSnapshot,
    MemoryScope,
    MemoryTopic,
)


class MemoryStoreBase:
    workspace_root: Path
    redactor: Optional[SecretRedactor]
    _session_entries: list[MemoryEntry]
    _lock: threading.RLock

    @property
    def memory_dir(self) -> Path:
        raise NotImplementedError

    @property
    def index_path(self) -> Path:
        raise NotImplementedError

    @property
    def topics_dir(self) -> Path:
        raise NotImplementedError

    @property
    def user_index_path(self) -> Path:
        raise NotImplementedError

    @property
    def user_topics_dir(self) -> Path:
        raise NotImplementedError

    @property
    def user_memory_dir(self) -> Path:
        raise NotImplementedError

    def scope_path(self, scope: MemoryScope | str) -> Optional[Path]:
        raise NotImplementedError

    def _safe_scope_path(
        self, scope: MemoryScope | str, path: Optional[Path]
    ) -> bool:
        raise NotImplementedError

    @staticmethod
    def _coerce_scopes(
        scopes: Optional[Iterable[MemoryScope | str]],
    ) -> tuple[MemoryScope, ...]:
        raise NotImplementedError

    def _load_scope(self, scope: MemoryScope) -> list[MemoryEntry]:
        raise NotImplementedError

    def list(
        self,
        *,
        limit: int = 20,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
    ) -> tuple[MemoryEntry, ...]:
        raise NotImplementedError

    def _write_scope(self, path: Path, entries: Iterable[MemoryEntry]) -> None:
        raise NotImplementedError

    def _safe_generated_path(self, path: Path, *, root: Path) -> bool:
        raise NotImplementedError

    def _atomic_write_text(
        self, path: Path, content: str, *, root: Path
    ) -> None:
        raise NotImplementedError

    def memory_topics(
        self,
        *,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
    ) -> tuple[MemoryTopic, ...]:
        raise NotImplementedError

    def index_snapshot(
        self,
        *,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
        max_entries: int = 20,
        max_chars: int = 9_000,
    ) -> MemoryIndexSnapshot:
        raise NotImplementedError

    @staticmethod
    def _render_topic_markdown(topic: MemoryTopic) -> str:
        raise NotImplementedError

    def _refresh_view_root(
        self,
        snapshot: MemoryIndexSnapshot,
        *,
        root: Path,
        index_path: Path,
        topics_dir: Path,
        write_topics: bool = True,
    ) -> None:
        raise NotImplementedError

    def _view_snapshots(
        self,
    ) -> tuple[MemoryIndexSnapshot, MemoryIndexSnapshot]:
        raise NotImplementedError

    def refresh_markdown_views(self) -> MemoryIndexSnapshot:
        raise NotImplementedError

    def refresh_index_views(self) -> None:
        raise NotImplementedError

    def refresh_topic_for_entry(
        self, entry: MemoryEntry
    ) -> Optional[MemoryTopic]:
        raise NotImplementedError


__all__ = ["MemoryStoreBase"]
