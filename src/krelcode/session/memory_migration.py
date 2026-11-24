"""Idempotent migration and non-secret memory diagnostics."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Optional

from .memory_base import MemoryStoreBase
from .memory_models import (
    MEMORY_SCOPE_ORDER,
    MemoryEntry,
    MemoryMigrationReport,
    MemoryScope,
    coerce_memory_scope,
)
from .memory_utils import memory_digest

class MemoryStoreMigrationMixin(MemoryStoreBase):
    def migrate(
        self,
        scope: MemoryScope | str = MemoryScope.PROJECT,
    ) -> MemoryMigrationReport:
        """Normalize one memory file in place with an idempotent backup.

        Legacy project records commonly contain only ``id/text/created_at``.
        Migration fills the scoped schema, redacts values, removes exact text
        duplicates and preserves pin state. A backup is created only when the
        canonical file would actually change.
        """
        normalized_scope = coerce_memory_scope(scope)
        path = self.scope_path(normalized_scope)
        if path is None:
            return MemoryMigrationReport(
                scope=normalized_scope.value, path="in-memory")
        with self._lock:
            if not self._safe_scope_path(normalized_scope, path):
                raise OSError("记忆文件路径不安全或为符号链接")
            if not path.is_file():
                return MemoryMigrationReport(
                    scope=normalized_scope.value, path=str(path))
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise OSError(f"读取记忆文件失败: {exc}") from exc
            nonempty = [line for line in lines if line.strip()]
            original: list[str] = []
            original_skipped = 0
            for line in nonempty:
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    original_skipped += 1
                    continue
                if not isinstance(payload, dict):
                    original_skipped += 1
                    continue
                original.append(json.dumps(
                    payload, ensure_ascii=False, sort_keys=True))

            loaded = self._load_scope(normalized_scope)
            by_digest: dict[str, MemoryEntry] = {}
            ordered: list[MemoryEntry] = []
            duplicates = 0
            for entry in loaded:
                digest = memory_digest(entry.text)
                previous = by_digest.get(digest)
                if previous is None:
                    by_digest[digest] = entry
                    ordered.append(entry)
                    continue
                duplicates += 1
                if entry.pinned and not previous.pinned:
                    replacement = MemoryEntry(
                        id=previous.id,
                        text=previous.text,
                        created_at=previous.created_at,
                        scope=previous.scope,
                        kind=previous.kind,
                        source=previous.source,
                        tags=tuple(dict.fromkeys(
                            (*previous.tags, *entry.tags))),
                        updated_at=max(previous.updated_at, entry.updated_at),
                        metadata={**entry.metadata, **previous.metadata},
                        pinned=True,
                        memory_type=previous.memory_type,
                        topic=previous.topic,
                        why=previous.why,
                        how_to_apply=previous.how_to_apply,
                    )
                    index = ordered.index(previous)
                    ordered[index] = replacement
                    by_digest[digest] = replacement

            skipped = max(0, len(nonempty) - len(loaded))
            migrated_lines = [
                json.dumps(
                    entry.as_record(), ensure_ascii=False, sort_keys=True)
                for entry in ordered
            ]
            changed = (
                original != migrated_lines
                or original_skipped != 0
                or duplicates != 0
            )
            backup_path: Optional[Path] = None
            if changed:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                candidate = path.with_name(f"{path.name}.bak-{stamp}")
                suffix = 1
                while candidate.exists():
                    candidate = path.with_name(
                        f"{path.name}.bak-{stamp}-{suffix}")
                    suffix += 1
                backup_path = candidate
                try:
                    shutil.copy2(path, backup_path)
                    self._write_scope(path, ordered)
                    if normalized_scope is MemoryScope.PROJECT:
                        self.refresh_markdown_views()
                except OSError as exc:
                    raise OSError(f"迁移记忆文件失败: {exc}") from exc
            return MemoryMigrationReport(
                scope=normalized_scope.value,
                path=str(path),
                scanned=len(nonempty),
                migrated=len(ordered) if changed else 0,
                kept=len(ordered),
                duplicates=duplicates,
                skipped=skipped,
                changed=changed,
                backup_path=str(backup_path) if backup_path is not None else "",
            )

    def scope_summary(self) -> dict[str, dict[str, object]]:
        """Return non-secret diagnostics for commands and UI."""
        summary: dict[str, dict[str, object]] = {}
        for scope in MEMORY_SCOPE_ORDER:
            entries = self.list(limit=0, scopes=(scope,))
            summary[scope.value] = {
                "entries": len(entries),
                "path": (
                    str(self.scope_path(scope))
                    if self.scope_path(scope) is not None else "in-memory"
                ),
            }
        return summary
