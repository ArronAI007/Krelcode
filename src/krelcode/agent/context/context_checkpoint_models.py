"""Checkpoint DTOs plus bounded file-integrity primitives."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, cast

from krelcode.agent.context.context_checkpoint_ports import WorkingContextPort
from krelcode.agent.context.context_plan import ContextPlanSnapshot, history_digest
from krelcode.agent.value_utils import nonnegative_float, nonnegative_int
from krelcode.core.codec import history_from_dicts, history_to_dicts
from krelcode.core.messages import Message


CONTEXT_CHECKPOINT_DIRNAME = Path(".krelcode") / "context-checkpoints"
CONTEXT_CHECKPOINT_VERSION = 1
MAX_TRACKED_FILES = 256
MAX_TREE_ENTRIES = 5_000


@dataclass(frozen=True)
class FileHash:
    path: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "FileHash":
        return cls(
            path=str(raw.get("path", "") or ""),
            sha256=str(raw.get("sha256", "") or ""),
            size=nonnegative_int(raw.get("size", 0)),
        )


@dataclass(frozen=True)
class MemoryReference:
    id: str
    digest: str
    scope: str
    kind: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "digest": self.digest,
            "scope": self.scope,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class WorkspaceFingerprint:
    branch: str = ""
    head: str = ""
    status_digest: str = ""
    tree_digest: str = ""
    rules_digest: str = ""
    rules_sources: tuple[str, ...] = ()
    managed_digest: str = ""
    captured_at: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head": self.head,
            "status_digest": self.status_digest,
            "tree_digest": self.tree_digest,
            "rules_digest": self.rules_digest,
            "rules_sources": list(self.rules_sources),
            "managed_digest": self.managed_digest,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> "WorkspaceFingerprint":
        if not isinstance(raw, Mapping):
            return cls()
        sources = raw.get("rules_sources")
        return cls(
            branch=str(raw.get("branch", "") or ""),
            head=str(raw.get("head", "") or ""),
            status_digest=str(raw.get("status_digest", "") or ""),
            tree_digest=str(raw.get("tree_digest", "") or ""),
            rules_digest=str(raw.get("rules_digest", "") or ""),
            rules_sources=tuple(
                str(item) for item in (sources if isinstance(sources, list) else ())
                if str(item)
            ),
            managed_digest=str(raw.get("managed_digest", "") or ""),
            captured_at=nonnegative_float(raw.get("captured_at", 0.0)),
        )


@dataclass(frozen=True)
class FileIntegrity:
    unchanged: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def stale(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.changed, *self.missing)))

    @property
    def valid(self) -> bool:
        return not self.changed and not self.missing

    def as_dict(self) -> dict[str, object]:
        return {
            "unchanged": list(self.unchanged),
            "changed": list(self.changed),
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class ContextRestoreResult:
    checkpoint_id: str
    session_id: str
    turn_id: str
    history_messages: int
    compact_checkpoint_id: str
    working_context_entries: int
    integrity: FileIntegrity
    memory_ids: tuple[str, ...] = ()
    workspace_refresh_pending: bool = False
    context_plan: ContextPlanSnapshot = field(default_factory=ContextPlanSnapshot)

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "history_messages": self.history_messages,
            "compact_checkpoint_id": self.compact_checkpoint_id,
            "working_context_entries": self.working_context_entries,
            "integrity": self.integrity.as_dict(),
            "memory_ids": list(self.memory_ids),
            "workspace_refresh_pending": self.workspace_refresh_pending,
            "context_plan": self.context_plan.as_dict(),
        }


@dataclass(frozen=True)
class ContextCheckpoint:
    id: str
    created_at: float
    session_id: str
    turn_id: str
    phase: str
    history: tuple[Message, ...]
    working_context: dict[str, object] = field(default_factory=dict)
    file_hashes: tuple[FileHash, ...] = ()
    workspace: WorkspaceFingerprint = field(default_factory=WorkspaceFingerprint)
    compact_checkpoint_id: str = ""
    compact_summary: dict[str, object] = field(default_factory=dict)
    memory_query: str = ""
    memory_refs: tuple[MemoryReference, ...] = ()
    context_plan: ContextPlanSnapshot = field(default_factory=ContextPlanSnapshot)
    plan: dict[str, object] = field(default_factory=dict)
    copied_from: str = ""
    parent_checkpoint_id: str = ""

    @property
    def history_hash(self) -> str:
        return history_digest(self.history)

    def metadata_record(self) -> dict[str, object]:
        """Content-free record suitable for the session JSONL audit stream."""
        working_entries = self.working_context.get("entries")
        working_kinds: dict[str, int] = {}
        if isinstance(working_entries, list):
            for entry in working_entries:
                if not isinstance(entry, Mapping):
                    continue
                kind = str(entry.get("kind", "") or "")
                if kind:
                    working_kinds[kind] = working_kinds.get(kind, 0) + 1
        return {
            "id": self.id,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "phase": self.phase,
            "history_hash": self.history_hash,
            "history_messages": len(self.history),
            "working_context": {
                "entries": (
                    len(working_entries)
                    if isinstance(working_entries, list) else 0
                ),
                "kinds": working_kinds,
                "sequence": self.working_context.get("sequence", 0),
                "tokens": self.working_context.get("tokens", 0),
                "chars": self.working_context.get("chars", 0),
                "truncated": bool(self.working_context.get("truncated", False)),
            },
            "file_count": len(self.file_hashes),
            "workspace": self.workspace.as_dict(),
            "compact_checkpoint_id": self.compact_checkpoint_id,
            "memory_query": self.memory_query,
            "memory_ids": [item.id for item in self.memory_refs],
            "context_plan": self.context_plan.as_dict(),
            "plan": dict(self.plan),
            "copied_from": self.copied_from,
            "parent_checkpoint_id": self.parent_checkpoint_id,
        }

    def as_payload(self) -> dict[str, object]:
        return {
            "type": "context_checkpoint",
            "version": CONTEXT_CHECKPOINT_VERSION,
            **self.metadata_record(),
            "working_context": dict(self.working_context),
            "history": history_to_dicts(self.history),
            "file_hashes": [item.as_dict() for item in self.file_hashes],
            "compact_summary": dict(self.compact_summary),
            "memory_refs": [item.as_dict() for item in self.memory_refs],
        }

    def verify_files(self, workspace_root: Path) -> FileIntegrity:
        return verify_file_hashes(self.file_hashes, Path(workspace_root))

    @classmethod
    def from_payload(cls, raw: Mapping[str, object], *, fallback_id: str = "") -> "ContextCheckpoint":
        messages_raw = raw.get("history")
        if not isinstance(messages_raw, list):
            raise ValueError("context checkpoint history 缺失")
        history = tuple(history_from_dicts(messages_raw))
        hashes = []
        raw_hashes = raw.get("file_hashes")
        if isinstance(raw_hashes, list):
            for item in raw_hashes:
                if not isinstance(item, Mapping):
                    continue
                try:
                    hashes.append(FileHash.from_dict(item))
                except (TypeError, ValueError):
                    continue
        refs = []
        raw_refs = raw.get("memory_refs")
        working_raw = raw.get("working_context")
        compact_raw = raw.get("compact_summary")
        plan_raw = raw.get("plan")
        workspace_raw = raw.get("workspace")
        context_plan_raw = raw.get("context_plan")
        if isinstance(raw_refs, list):
            for item in raw_refs:
                if not isinstance(item, Mapping):
                    continue
                refs.append(MemoryReference(
                    id=str(item.get("id", "") or ""),
                    digest=str(item.get("digest", "") or ""),
                    scope=str(item.get("scope", "") or ""),
                    kind=str(item.get("kind", "") or ""),
                ))
        return cls(
            id=str(raw.get("id", fallback_id) or fallback_id),
            created_at=nonnegative_float(raw.get("created_at", 0.0)),
            session_id=str(raw.get("session_id", "") or ""),
            turn_id=str(raw.get("turn_id", "") or ""),
            phase=str(raw.get("phase", "end") or "end"),
            history=history,
            working_context=(
                dict(cast(Mapping[str, object], working_raw))
                if isinstance(working_raw, Mapping) else {}),
            file_hashes=tuple(hashes),
            workspace=WorkspaceFingerprint.from_dict(
                cast(Mapping[str, object], workspace_raw)
                if isinstance(workspace_raw, Mapping) else None),
            compact_checkpoint_id=str(raw.get("compact_checkpoint_id", "") or ""),
            compact_summary=(
                dict(cast(Mapping[str, object], compact_raw))
                if isinstance(compact_raw, Mapping) else {}),
            memory_query=str(raw.get("memory_query", "") or ""),
            memory_refs=tuple(refs),
            context_plan=ContextPlanSnapshot.from_dict(
                cast(Mapping[str, object], context_plan_raw)
                if isinstance(context_plan_raw, Mapping) else None),
            plan=(
                dict(cast(Mapping[str, object], plan_raw))
                if isinstance(plan_raw, Mapping) else {}),
            copied_from=str(raw.get("copied_from", "") or ""),
            parent_checkpoint_id=str(raw.get("parent_checkpoint_id", "") or ""),
        )


def collect_tracked_files(
    workspace_root: Path,
    history: Sequence[Message],
    working_context: Optional[WorkingContextPort] = None,
) -> tuple[Path, ...]:
    root = Path(workspace_root).resolve()
    candidates: list[Path] = []
    path_keys = {"path", "file", "file_path", "filename"}
    for message in history:
        for call in message.tool_calls or ():
            if call.name not in {
                "read_file", "write_file", "edit_file", "grep", "find_refs",
                "check_syntax",
            }:
                continue
            for key in path_keys:
                raw = call.arguments.get(key)
                if isinstance(raw, str):
                    resolved = _resolve_workspace_path(root, raw)
                    if resolved is not None:
                        candidates.append(resolved)
    if working_context is not None:
        for entry in working_context.entries:
            raw = entry.metadata.get("path") if entry.metadata else None
            if isinstance(raw, str):
                resolved = _resolve_workspace_path(root, raw)
                if resolved is not None:
                    candidates.append(resolved)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
        if len(unique) >= MAX_TRACKED_FILES:
            break
    return tuple(unique)


def hash_tracked_files(paths: Iterable[Path], workspace_root: Path) -> tuple[FileHash, ...]:
    root = Path(workspace_root).resolve()
    records = []
    for path in paths:
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_file() or (resolved != root and root not in resolved.parents):
            continue
        digest, size = _hash_file(resolved)
        if digest is None:
            continue
        records.append(FileHash(
            path=resolved.relative_to(root).as_posix(),
            sha256=digest,
            size=size,
        ))
    return tuple(records)


def verify_file_hashes(
    expected: Sequence[FileHash],
    workspace_root: Path,
) -> FileIntegrity:
    root = Path(workspace_root).resolve()
    unchanged: list[str] = []
    changed: list[str] = []
    missing: list[str] = []
    for record in expected:
        target = _resolve_workspace_path(root, record.path)
        if target is None or not target.is_file():
            missing.append(record.path)
            continue
        digest, _ = _hash_file(target)
        if digest is None:
            missing.append(record.path)
            continue
        (unchanged if digest == record.sha256 else changed).append(record.path)
    return FileIntegrity(tuple(unchanged), tuple(changed), tuple(missing))



def _hash_file(path: Path) -> tuple[Optional[str], int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None, 0
    return digest.hexdigest(), size


def _resolve_workspace_path(root: Path, raw: str) -> Optional[Path]:
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved




__all__ = [
    "CONTEXT_CHECKPOINT_DIRNAME",
    "CONTEXT_CHECKPOINT_VERSION",
    "MAX_TRACKED_FILES",
    "MAX_TREE_ENTRIES",
    "ContextCheckpoint",
    "ContextRestoreResult",
    "FileHash",
    "FileIntegrity",
    "MemoryReference",
    "WorkspaceFingerprint",
    "collect_tracked_files",
    "hash_tracked_files",
    "verify_file_hashes",
]
