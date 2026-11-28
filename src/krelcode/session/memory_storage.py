"""Authoritative scoped JSONL storage and bounded entry mutation."""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import ContextManager, Iterable, List, Mapping, Optional

from ..core.redact import MASK, SecretRedactor
from ..core.file_lock import InterProcessFileLock
from ..contracts.asset import AssetEnvelope
from .memory_base import MemoryStoreBase
from .memory_models import (
    INJECT_ENTRIES,
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    MAX_TAG_CHARS,
    MAX_TOPIC_CHARS,
    MAX_WHY_CHARS,
    MEMORY_SCOPE_ORDER,
    STRUCTURED_MEMORY_TYPES,
    _SAFE_SESSION_ID_RE,
    _SENSITIVE_METADATA_KEY_RE,
    MemoryClass,
    MemoryEntry,
    MemoryKind,
    MemoryRevisionConflict,
    MemoryScope,
    MemoryType,
    coerce_memory_kind,
    coerce_memory_scope,
    coerce_memory_type,
    default_user_memory_path,
    memory_lifecycle_score,
    validate_memory_structure,
)
from .memory_utils import (
    memory_digest,
    normalize_memory_text,
    sanitize_memory_text,
)

class MemoryStoreStorageMixin(MemoryStoreBase):
    """Manage user/project/session memory with common bounds and redaction."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        redactor: Optional[SecretRedactor] = None,
        path: Optional[Path] = None,
        user_path: Optional[Path] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.project_path = Path(path) if path is not None else (
            self.workspace_root / ".krelcode" / "memory" / "entries.jsonl")
        # ``path`` is retained as the historical project-store override.
        self.path = self.project_path
        self.user_path = (
            Path(user_path).expanduser()
            if user_path is not None else default_user_memory_path())
        self.redactor = redactor
        self.writer_id = f"memory-{uuid.uuid4().hex[:12]}"
        self._session_id: Optional[str] = None
        self._session_entries: list[MemoryEntry] = []
        self._session_revision = 0
        self._lock = threading.RLock()
        if session_id is not None:
            self.bind_session(session_id)

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def memory_dir(self) -> Path:
        return self.project_path.parent

    @property
    def index_path(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    @property
    def topics_dir(self) -> Path:
        return self.memory_dir / "topics"

    @property
    def user_index_path(self) -> Path:
        return self.user_path.parent / "MEMORY.md"

    @property
    def user_topics_dir(self) -> Path:
        return self.user_path.parent / "topics"

    @property
    def user_memory_dir(self) -> Path:
        return self.user_path.parent

    @property
    def session_path(self) -> Optional[Path]:
        if self._session_id is None:
            return None
        return self.project_path.parent / "sessions" / f"{self._session_id}.jsonl"

    def revision(self, scope: MemoryScope | str = MemoryScope.PROJECT) -> int:
        normalized = coerce_memory_scope(scope)
        if normalized is MemoryScope.SESSION:
            return self._session_revision
        path = self.scope_path(normalized)
        if path is None:
            return self._session_revision
        revision_path = path.with_suffix(path.suffix + ".revision")
        try:
            return max(0, int(revision_path.read_text(
                encoding="utf-8").strip()))
        except (OSError, ValueError):
            return 0

    def _assert_revision(
        self,
        scope: MemoryScope,
        expected: Optional[int],
    ) -> int:
        current = self.revision(scope)
        if expected is not None and int(expected) != current:
            raise MemoryRevisionConflict(scope, int(expected), current)
        return current

    def _set_revision(self, scope: MemoryScope, revision: int) -> None:
        if scope is MemoryScope.SESSION:
            self._session_revision = max(0, int(revision))
            return
        path = self.scope_path(scope)
        if path is None:
            self._session_revision = max(0, int(revision))
            return
        revision_path = path.with_suffix(path.suffix + ".revision")
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        temp = revision_path.with_name(
            f".{revision_path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            temp.write_text(f"{max(0, int(revision))}\n", encoding="utf-8")
            temp.replace(revision_path)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def _scope_lock(self, scope: MemoryScope) -> ContextManager[object]:
        path = self.scope_path(scope)
        if path is None:
            return nullcontext()
        return InterProcessFileLock(
            path.with_name(path.name + ".lock"))

    def bind_session(self, session_id: str) -> None:
        """Attach this store to a persisted session and flush pending entries."""
        normalized = _SAFE_SESSION_ID_RE.sub("_", str(session_id).strip())[:128]
        if not normalized or normalized in {".", ".."}:
            raise ValueError("session_id 不能为空或包含非法路径片段")
        with self._lock:
            if normalized == self._session_id:
                return
            pending = tuple(self._session_entries)
            self._session_id = normalized
            self._session_entries = []
            for entry in pending:
                self._append_entry(entry)
            if pending:
                self._set_revision(
                    MemoryScope.SESSION, self._session_revision + 1)
                try:
                    self.refresh_markdown_views()
                except OSError:
                    pass

    def copy_session(self, source_session_id: str, target_session_id: str) -> bool:
        """Copy session-scoped memory into a fork without sharing future writes."""
        source = _SAFE_SESSION_ID_RE.sub(
            "_", str(source_session_id).strip())[:128]
        target = _SAFE_SESSION_ID_RE.sub(
            "_", str(target_session_id).strip())[:128]
        if not source or not target or source == target:
            return False
        source_path = self.project_path.parent / "sessions" / f"{source}.jsonl"
        target_path = self.project_path.parent / "sessions" / f"{target}.jsonl"
        if (
            not self._safe_scope_path(MemoryScope.SESSION, source_path)
            or not self._safe_scope_path(MemoryScope.SESSION, target_path)
        ):
            return False
        if not source_path.is_file():
            return False
        if target_path.exists():
            return True
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)
        except OSError:
            return False
        return True

    def scope_path(self, scope: MemoryScope | str) -> Optional[Path]:
        normalized = coerce_memory_scope(scope)
        if normalized is MemoryScope.USER:
            return self.user_path
        if normalized is MemoryScope.PROJECT:
            return self.project_path
        return self.session_path

    def _safe_scope_path(
        self,
        scope: MemoryScope | str,
        path: Optional[Path],
    ) -> bool:
        """Reject final-component and parent-directory symlink escapes."""
        if path is None:
            return True
        normalized = coerce_memory_scope(scope)
        try:
            if path.is_symlink() or path.parent.is_symlink():
                return False
            if normalized in {MemoryScope.PROJECT, MemoryScope.SESSION}:
                root = self.workspace_root.resolve()
                resolved = path.resolve(strict=False)
                if resolved != root and root not in resolved.parents:
                    return False
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _sanitize(self, text: str) -> str:
        normalized = " ".join(str(text).split())
        if not normalized:
            raise ValueError("记忆内容不能为空")
        if len(normalized) > MAX_ENTRY_CHARS:
            raise ValueError(f"记忆内容不能超过 {MAX_ENTRY_CHARS} 字符")
        return sanitize_memory_text(
            normalized, redactor=self.redactor, max_chars=MAX_ENTRY_CHARS)

    def _sanitize_tag(self, value: object) -> str:
        tag = " ".join(str(value).split())[:MAX_TAG_CHARS]
        if not tag:
            return ""
        return sanitize_memory_text(
            tag, redactor=self.redactor, max_chars=MAX_TAG_CHARS)

    def _sanitize_metadata(self, value: object, *, depth: int = 0) -> object:
        """Recursively redact bounded metadata values before persistence."""
        if depth >= 4:
            return MASK
        if isinstance(value, str):
            if not value.strip():
                return ""
            return sanitize_memory_text(
                value, redactor=self.redactor, max_chars=MAX_ENTRY_CHARS)
        if isinstance(value, Mapping):
            sanitized: dict[str, object] = {}
            for key, item in list(value.items())[:64]:
                if not str(key).strip():
                    continue
                safe_key = sanitize_memory_text(
                    str(key), redactor=self.redactor, max_chars=MAX_TAG_CHARS)
                if safe_key:
                    sanitized[safe_key] = (
                        MASK if _SENSITIVE_METADATA_KEY_RE.search(safe_key)
                        else self._sanitize_metadata(item, depth=depth + 1)
                    )
            return sanitized
        if isinstance(value, (list, tuple)):
            return [
                self._sanitize_metadata(item, depth=depth + 1)
                for item in list(value)[:64]
            ]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return sanitize_memory_text(
            str(value), redactor=self.redactor, max_chars=MAX_ENTRY_CHARS)

    @staticmethod
    def _coerce_scopes(
        scopes: Optional[Iterable[MemoryScope | str]],
    ) -> tuple[MemoryScope, ...]:
        if scopes is None:
            return MEMORY_SCOPE_ORDER
        selected = {coerce_memory_scope(scope) for scope in scopes}
        return tuple(scope for scope in MEMORY_SCOPE_ORDER if scope in selected)

    def _load_scope(self, scope: MemoryScope) -> list[MemoryEntry]:
        path = self.scope_path(scope)
        if path is None:
            return list(self._session_entries)
        if not self._safe_scope_path(scope, path):
            return []
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[MemoryEntry] = []
        for line_number, line in enumerate(lines):
            try:
                payload = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            text = payload.get("text")
            entry_id = payload.get("id")
            created_at = payload.get("created_at", "")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                safe_text = self._sanitize(text)
            except ValueError:
                continue
            raw_tags = payload.get("tags")
            if not isinstance(raw_tags, list):
                raw_tags = ()
            tags = tuple(
                tag for raw in raw_tags
                if (tag := self._sanitize_tag(raw))
            )
            raw_metadata = payload.get("metadata")
            sanitized_metadata = (
                self._sanitize_metadata(raw_metadata)
                if isinstance(raw_metadata, dict) else {}
            )
            metadata: Mapping[str, object] = (
                sanitized_metadata
                if isinstance(sanitized_metadata, Mapping)
                else {}
            )
            raw_asset = payload.get("asset")
            asset = (
                AssetEnvelope.from_dict(raw_asset)
                if isinstance(raw_asset, Mapping) else None
            )
            try:
                kind = coerce_memory_kind(payload.get("kind"))
            except ValueError:
                # Unknown future kinds must not make an otherwise valid note
                # disappear when an older client reads the file.
                kind = MemoryKind.NOTE
            try:
                memory_type = coerce_memory_type(
                    payload.get("memory_type") or MemoryType.REFERENCE)
            except ValueError:
                memory_type = MemoryType.REFERENCE
            why = ""
            how_to_apply = ""
            for key, target in (
                ("why", "why"),
                ("how_to_apply", "how"),
            ):
                raw_value = payload.get(key)
                if isinstance(raw_value, str) and raw_value.strip():
                    safe_value = sanitize_memory_text(
                        raw_value,
                        redactor=self.redactor,
                        max_chars=MAX_WHY_CHARS,
                    )
                    if target == "why":
                        why = safe_value
                    else:
                        how_to_apply = safe_value
            try:
                entry = MemoryEntry(
                    id=str(entry_id or f"legacy-{scope.value}-{line_number}"),
                    text=safe_text,
                    created_at=str(created_at),
                    # File location is authoritative: a forged record cannot
                    # smuggle itself into another scope by editing this field.
                    scope=scope,
                    kind=kind,
                    source=str(payload.get("source") or "manual"),
                    tags=tags,
                    updated_at=str(payload.get("updated_at") or created_at),
                    metadata=metadata,
                    pinned=bool(payload.get("pinned", False)),
                    memory_type=memory_type,
                    topic=str(payload.get("topic") or "")[:MAX_TOPIC_CHARS],
                    why=why,
                    how_to_apply=how_to_apply,
                    revision=max(1, int(payload.get("revision") or 1)),
                    writer_id=str(payload.get("writer_id") or ""),
                    memory_class=str(
                        payload.get("memory_class") or "semantic"),
                    last_verified_at=str(
                        payload.get("last_verified_at") or ""),
                    use_count=max(0, int(payload.get("use_count") or 0)),
                    success_count=max(
                        0, int(payload.get("success_count") or 0)),
                    failure_count=max(
                        0, int(payload.get("failure_count") or 0)),
                    last_outcome=str(payload.get("last_outcome") or ""),
                    asset=asset,
                )
            except (TypeError, ValueError):
                continue
            entries.append(entry)
        return entries

    def list(
        self,
        *,
        limit: int = INJECT_ENTRIES,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
    ) -> tuple[MemoryEntry, ...]:
        """Return merged entries in chronological order (oldest first)."""
        selected = self._coerce_scopes(scopes)
        with self._lock:
            ordered: list[tuple[str, int, int, MemoryEntry]] = []
            for scope_rank, scope in enumerate(selected):
                for index, entry in enumerate(self._load_scope(scope)):
                    ordered.append((
                        entry.created_at, scope_rank, index, entry))
        ordered.sort(key=lambda item: item[:3])
        entries = [item[3] for item in ordered]
        if limit > 0:
            entries = entries[-limit:]
        return tuple(entries)

    def _append_entry(self, entry: MemoryEntry) -> None:
        scope = coerce_memory_scope(entry.scope)
        path = self.scope_path(scope)
        if path is None:
            self._session_entries.append(entry)
            if len(self._session_entries) > MAX_ENTRIES:
                self._session_entries = self._bounded_entries(
                    self._session_entries)
            return
        if not self._safe_scope_path(scope, path):
            raise ValueError("记忆文件路径不安全或为符号链接")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                entry.as_record(), ensure_ascii=False, sort_keys=True) + "\n")

    def add(
        self,
        text: str,
        *,
        scope: MemoryScope | str = MemoryScope.PROJECT,
        kind: MemoryKind | str = MemoryKind.NOTE,
        memory_type: MemoryType | str = MemoryType.REFERENCE,
        topic: str = "",
        why: str = "",
        how_to_apply: str = "",
        tags: Iterable[str] = (),
        source: str = "manual",
        metadata: Optional[Mapping[str, object]] = None,
        pinned: bool = False,
        deduplicate: bool = False,
        expected_revision: Optional[int] = None,
        memory_class: MemoryClass | str = MemoryClass.SEMANTIC,
    ) -> MemoryEntry:
        normalized_scope = coerce_memory_scope(scope)
        normalized_type = coerce_memory_type(memory_type)
        safe_text = self._sanitize(text)
        safe_why = ""
        safe_how = ""
        if str(why).strip():
            safe_why = sanitize_memory_text(
                why, redactor=self.redactor, max_chars=MAX_WHY_CHARS)
        if str(how_to_apply).strip():
            safe_how = sanitize_memory_text(
                how_to_apply,
                redactor=self.redactor,
                max_chars=MAX_WHY_CHARS,
            )
        validate_memory_structure(normalized_type, safe_why, safe_how)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        safe_metadata = self._sanitize_metadata(dict(metadata or {}))
        entry = MemoryEntry(
            id=f"{normalized_scope.value}-{uuid.uuid4().hex[:12]}",
            text=safe_text,
            created_at=now,
            scope=normalized_scope,
            kind=coerce_memory_kind(kind),
            memory_type=normalized_type,
            topic=topic,
            why=safe_why,
            how_to_apply=safe_how,
            source=source,
            tags=tuple(self._sanitize_tag(tag) for tag in tags),
            metadata=safe_metadata if isinstance(safe_metadata, dict) else {},
            pinned=pinned,
            writer_id=self.writer_id,
            memory_class=memory_class,
        )
        with self._lock, self._scope_lock(normalized_scope):
            current_revision = self._assert_revision(
                normalized_scope, expected_revision)
            if deduplicate:
                digest = memory_digest(safe_text)
                for existing in self._load_scope(normalized_scope):
                    if memory_digest(existing.text) == digest:
                        return existing
            entry = replace(
                entry,
                revision=current_revision + 1,
                writer_id=self.writer_id,
            )
            self._append_entry(entry)
            try:
                self._compact_if_needed(normalized_scope)
            except OSError:
                # The append already succeeded; compaction is best-effort.
                pass
            self._set_revision(normalized_scope, current_revision + 1)
            try:
                self.refresh_index_views()
                self.refresh_topic_for_entry(entry)
            except OSError:
                # JSONL remains authoritative. A later mutation or explicit
                # refresh can regenerate the Markdown views.
                pass
        return entry

    def _replace_entry(
        self,
        entry: MemoryEntry,
        *,
        expected_revision: Optional[int] = None,
    ) -> MemoryEntry:
        """Atomically replace one entry in its authoritative scope."""
        scope = coerce_memory_scope(entry.scope)
        path = self.scope_path(scope)
        with self._lock, self._scope_lock(scope):
            current_revision = self._assert_revision(
                scope, expected_revision)
            entries = self._load_scope(scope)
            if not any(item.id == entry.id for item in entries):
                raise KeyError(entry.id)
            next_revision = entry.revision + 1
            asset = (
                replace(entry.asset, version=str(next_revision))
                if entry.asset is not None else None
            )
            entry = replace(
                entry,
                revision=next_revision,
                writer_id=self.writer_id,
                asset=asset,
            )
            updated = [
                entry if item.id == entry.id else item for item in entries]
            if path is None:
                self._session_entries = updated
            else:
                self._write_scope(path, updated)
            self._set_revision(scope, current_revision + 1)
            try:
                self.refresh_index_views()
            except OSError:
                pass
        return entry

    def set_pinned(
        self,
        entry_id: str,
        pinned: bool = True,
        *,
        scope: Optional[MemoryScope | str] = None,
        expected_revision: Optional[int] = None,
    ) -> Optional[MemoryEntry]:
        """Pin/unpin a memory; pinned entries rank first in retrieval."""
        target = str(entry_id).strip()
        if not target:
            return None
        selected = (
            (coerce_memory_scope(scope),)
            if scope is not None else MEMORY_SCOPE_ORDER)
        for current_scope in selected:
            entry = next(
                (item for item in self._load_scope(current_scope)
                 if item.id == target),
                None,
            )
            if entry is None:
                continue
            updated_at = datetime.now().astimezone().isoformat(
                timespec="seconds")
            return self._replace_entry(MemoryEntry(
                id=entry.id,
                text=entry.text,
                created_at=entry.created_at,
                scope=entry.scope,
                kind=entry.kind,
                source=entry.source,
                tags=entry.tags,
                updated_at=updated_at,
                metadata=entry.metadata,
                pinned=bool(pinned),
                memory_type=entry.memory_type,
                topic=entry.topic,
                why=entry.why,
                how_to_apply=entry.how_to_apply,
                revision=entry.revision,
                writer_id=entry.writer_id,
                memory_class=entry.memory_class,
                last_verified_at=entry.last_verified_at,
                use_count=entry.use_count,
                success_count=entry.success_count,
                failure_count=entry.failure_count,
                last_outcome=entry.last_outcome,
                asset=entry.asset,
            ), expected_revision=expected_revision)
        return None

    def pin(
        self,
        entry_id: str,
        *,
        scope: Optional[MemoryScope | str] = None,
        expected_revision: Optional[int] = None,
    ) -> Optional[MemoryEntry]:
        return self.set_pinned(
            entry_id, True, scope=scope,
            expected_revision=expected_revision)

    def unpin(
        self,
        entry_id: str,
        *,
        scope: Optional[MemoryScope | str] = None,
        expected_revision: Optional[int] = None,
    ) -> Optional[MemoryEntry]:
        return self.set_pinned(
            entry_id, False, scope=scope,
            expected_revision=expected_revision)

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        scopes: Optional[Iterable[MemoryScope | str]] = None,
    ) -> tuple[MemoryEntry, ...]:
        """Rank bounded memory retrieval by query relevance and pin state."""
        normalized_query = normalize_memory_text(query)
        terms = [
            term for term in re.findall(
                r"[a-z0-9_.:/-]{2,}|[\u4e00-\u9fff]{2,}",
                normalized_query,
            )
        ]
        entries = self.list(limit=0, scopes=scopes)
        ranked: list[tuple[bool, float, str, int, MemoryEntry]] = []
        for index, entry in enumerate(entries):
            haystack = normalize_memory_text(
                " ".join((
                    entry.text,
                    *entry.tags,
                    entry.kind_value,
                    entry.memory_type_value,
                    entry.topic,
                    entry.why,
                    entry.how_to_apply,
                )))
            score = 0.0
            if not normalized_query:
                score = 1.0
            else:
                if normalized_query in haystack:
                    score += 6.0
                score += sum(2.0 for term in terms if term in haystack)
            if score <= 0:
                continue
            if coerce_memory_kind(entry.kind) is MemoryKind.KNOWLEDGE:
                score += 0.5
            if coerce_memory_type(entry.memory_type) in STRUCTURED_MEMORY_TYPES:
                score += 0.25
            score += memory_lifecycle_score(entry) * 2.0
            ranked.append((
                entry.pinned, score, entry.updated_at, index, entry))
        ranked.sort(
            key=lambda item: (item[0], item[1], item[2], item[3]),
            reverse=True,
        )
        limited = [item[4] for item in ranked]
        if limit > 0:
            limited = limited[:limit]
        return tuple(limited)

    def _write_scope(self, path: Path, entries: Iterable[MemoryEntry]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        try:
            temp.write_text(
                "".join(
                    json.dumps(
                        entry.as_record(), ensure_ascii=False, sort_keys=True)
                    + "\n"
                    for entry in entries
                ),
                encoding="utf-8",
            )
            temp.replace(path)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def _compact_if_needed(self, scope: MemoryScope) -> None:
        path = self.scope_path(scope)
        if path is None or not self._safe_scope_path(scope, path):
            return
        try:
            count = sum(
                1 for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError:
            return
        if count <= MAX_ENTRIES:
            return
        entries = self._bounded_entries(
            self.list(limit=0, scopes=(scope,)))
        self._write_scope(path, entries)

    @staticmethod
    def _bounded_entries(entries: Iterable[MemoryEntry]) -> List[MemoryEntry]:
        """Keep pinned entries while enforcing the per-scope record bound."""
        items = list(entries)
        pinned = [item for item in items if item.pinned]
        unpinned = [item for item in items if not item.pinned]
        if len(pinned) >= MAX_ENTRIES:
            selected = pinned[-MAX_ENTRIES:]
        else:
            selected = pinned + unpinned[-(MAX_ENTRIES - len(pinned)):]
        selected.sort(key=lambda item: (item.created_at, item.id))
        return selected

    def _safe_generated_path(self, path: Path, *, root: Path) -> bool:
        """Reject path traversal and symlink components for generated views."""
        try:
            root = Path(root).resolve()
            resolved = path.resolve(strict=False)
            if resolved != root and root not in resolved.parents:
                return False
            current = path
            while current != root and root in current.parents:
                if current.is_symlink():
                    return False
                current = current.parent
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _atomic_write_text(
        self, path: Path, content: str, *, root: Path,
    ) -> None:
        if not self._safe_generated_path(path, root=root):
            raise OSError(f"记忆视图路径不安全或为符号链接: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            temp.write_text(content, encoding="utf-8")
            temp.replace(path)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
