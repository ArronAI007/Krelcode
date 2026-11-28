"""Memory outcome feedback and authoritative deletion lifecycle."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from typing import Optional

from ..contracts.asset import AssetDeletionMode, AssetDeletionPlan
from ..governance.asset_registry import AssetDependencyGraph
from .memory_base import MemoryStoreBase
from .memory_models import (
    MEMORY_SCOPE_ORDER,
    MemoryEntry,
    MemoryScope,
    coerce_memory_scope,
)


class MemoryStoreLifecycleMixin(MemoryStoreBase):
    def _replace_entry(
        self,
        entry: MemoryEntry,
        *,
        expected_revision: Optional[int] = None,
    ) -> MemoryEntry:
        raise NotImplementedError

    def record_use(
        self,
        entry_id: str,
        *,
        success: bool,
        scope: Optional[MemoryScope | str] = None,
        verified_at: str = "",
    ) -> Optional[MemoryEntry]:
        """Record retrieval outcome and refresh lifecycle evidence."""
        target = str(entry_id).strip()
        if not target:
            return None
        selected = (
            (coerce_memory_scope(scope),)
            if scope is not None else MEMORY_SCOPE_ORDER)
        now = verified_at or datetime.now().astimezone().isoformat(
            timespec="seconds")
        for current_scope in selected:
            entry = next(
                (item for item in self._load_scope(current_scope)
                 if item.id == target),
                None,
            )
            if entry is None:
                continue
            return self._replace_entry(replace(
                entry,
                use_count=entry.use_count + 1,
                success_count=entry.success_count + (1 if success else 0),
                failure_count=entry.failure_count + (0 if success else 1),
                last_outcome="success" if success else "failure",
                last_verified_at=now if success else entry.last_verified_at,
            ))
        return None

    def deletion_plan(
        self,
        entry_id: str,
        *,
        mode: AssetDeletionMode | str = AssetDeletionMode.QUARANTINE,
    ) -> AssetDeletionPlan:
        assets = [
            entry.asset
            for scope in MEMORY_SCOPE_ORDER
            for entry in self._load_scope(scope)
            if entry.asset is not None
        ]
        return AssetDependencyGraph(assets).plan_delete(
            str(entry_id or "").strip(), mode=mode)

    def remove(
        self,
        entry_id: str,
        *,
        scope: Optional[MemoryScope | str] = None,
        expected_revision: Optional[int] = None,
    ) -> Optional[MemoryEntry]:
        """Remove one entry and refresh its derived Markdown views."""
        target = str(entry_id).strip()
        if not target:
            return None
        selected = (
            (coerce_memory_scope(scope),)
            if scope is not None else MEMORY_SCOPE_ORDER)
        if expected_revision is not None and len(selected) != 1:
            raise ValueError("expected_revision 要求显式指定 scope")
        with self._lock, ExitStack() as stack:
            for selected_scope in selected:
                stack.enter_context(self._scope_lock(selected_scope))
            for current_scope in selected:
                self._assert_revision(current_scope, expected_revision)
                entries = self._load_scope(current_scope)
                removed = next(
                    (entry for entry in entries if entry.id == target), None)
                if removed is None:
                    continue
                path = self.scope_path(current_scope)
                if path is None:
                    self._session_entries = [
                        entry for entry in entries if entry.id != target]
                else:
                    self._write_scope(path, (
                        entry for entry in entries if entry.id != target))
                self._set_revision(
                    current_scope, self.revision(current_scope) + 1)
                try:
                    self.refresh_markdown_views()
                except OSError:
                    pass
                return removed
        return None


__all__ = ["MemoryStoreLifecycleMixin"]
