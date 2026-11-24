"""Persistent full-context checkpoints for resume, fork and rewind.

The JSONL session remains the authoritative audit log.  A ContextCheckpoint is a
larger, atomically-written handoff document that lets a new process reconstruct
the *live context* rather than merely replaying messages:

* authoritative history and its digest;
* compact-checkpoint/summary reference;
* Working Context projection and ContextPlan;
* hashes for files touched by the task;
* live workspace fingerprints (git/tree/rules);
* task-filtered memory references (never full memory corpus copies).

Resume deliberately re-reads git, tree, rules, file hashes and memory from the
current workspace.  Historical fingerprints are evidence for invalidation, not
values to inject into the new prompt.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence, cast

from krelcode.agent.context.context_checkpoint_event import checkpoint_created_event
from krelcode.agent.context.context_checkpoint_ports import (
    CheckpointLoopPort,
    CompactStorePort,
    MemoryEntryPort,
    MemoryStorePort,
    WorkingContextPort,
)
from krelcode.agent.context.context_plan import ContextPlanSnapshot, history_digest
from krelcode.agent.context.memory_context_render import render_memory_context
from krelcode.agent.context.working_context import WorkingContextStore
from krelcode.agent.value_utils import nonnegative_float, nonnegative_int
from krelcode.core.events import (
    AgentEvent,
    ContextAssembled,
    EventBus,
    PlanUpdated,
    RunCancelled,
    RunFinished,
    RunStarted,
)
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor
from krelcode.core.process import process_isolation_kwargs
from krelcode.core.codec import history_from_dicts, history_to_dicts

CONTEXT_CHECKPOINT_DIRNAME = Path(".krelcode") / "context-checkpoints"
CONTEXT_CHECKPOINT_VERSION = 1
MAX_TRACKED_FILES = 256
MAX_TREE_ENTRIES = 5_000
_MANAGED_INSTRUCTION_RELPATH = Path(".krelcode") / "managed" / "AGENTS.md"


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


class ContextCheckpointStore:
    """Atomic, redacted, workspace-local ContextCheckpoint store."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        redactor: Optional[SecretRedactor] = None,
        directory: Optional[Path] = None,
    ) -> None:
        self.root = Path(workspace_root).resolve()
        self.dir = Path(directory) if directory is not None else (
            self.root / CONTEXT_CHECKPOINT_DIRNAME)
        self.redactor = redactor

    def record(
        self,
        *,
        history: Sequence[Message],
        session_id: str = "",
        turn_id: str = "",
        phase: str = "end",
        working_context: Optional[Mapping[str, object]] = None,
        file_hashes: Sequence[FileHash] = (),
        workspace: Optional[WorkspaceFingerprint] = None,
        compact_checkpoint_id: str = "",
        compact_summary: Optional[Mapping[str, object]] = None,
        memory_query: str = "",
        memory_refs: Sequence[MemoryReference] = (),
        context_plan: Optional[ContextPlanSnapshot] = None,
        plan: Optional[Mapping[str, object]] = None,
        copied_from: str = "",
        parent_checkpoint_id: str = "",
        created_at: Optional[float] = None,
    ) -> Optional[ContextCheckpoint]:
        timestamp = float(created_at if created_at is not None else time.time())
        checkpoint_id = self._next_id(timestamp)
        checkpoint = ContextCheckpoint(
            id=checkpoint_id,
            created_at=timestamp,
            session_id=str(session_id or ""),
            turn_id=str(turn_id or ""),
            phase=str(phase or "end"),
            history=tuple(copy.deepcopy(list(history))),
            working_context=dict(working_context or {}),
            file_hashes=tuple(file_hashes),
            workspace=workspace or WorkspaceFingerprint(captured_at=timestamp),
            compact_checkpoint_id=str(compact_checkpoint_id or ""),
            compact_summary=dict(compact_summary or {}),
            memory_query=str(memory_query or ""),
            memory_refs=tuple(memory_refs),
            context_plan=context_plan or ContextPlanSnapshot(),
            plan=dict(plan or {}),
            copied_from=str(copied_from or ""),
            parent_checkpoint_id=str(parent_checkpoint_id or ""),
        )
        return self._write(checkpoint)

    def copy_reference(
        self,
        source: ContextCheckpoint | str,
        *,
        session_id: str,
        parent_checkpoint_id: str = "",
    ) -> Optional[ContextCheckpoint]:
        checkpoint = source if isinstance(source, ContextCheckpoint) else self.load(str(source))
        if checkpoint is None:
            return None
        timestamp = time.time()
        copied = replace(
            checkpoint,
            id=self._next_id(timestamp),
            created_at=timestamp,
            session_id=str(session_id or ""),
            copied_from=checkpoint.id,
            parent_checkpoint_id=(
                str(parent_checkpoint_id or "")
                or checkpoint.parent_checkpoint_id
                or checkpoint.id
            ),
        )
        return self._write(copied)

    def load(self, checkpoint_id: str) -> Optional[ContextCheckpoint]:
        safe = "".join(
            char for char in str(checkpoint_id)
            if char.isalnum() or char in {"-", "_"}
        )
        if not safe:
            return None
        return self._load_file(self.dir / f"{safe}.json")

    def latest_for_session(self, session_id: str) -> Optional[ContextCheckpoint]:
        candidates = [
            item for item in self._load_all()
            if not session_id or item.session_id == session_id
        ]
        return max(candidates, key=lambda item: item.created_at) if candidates else None

    def latest_for_turn(self, session_id: str, turn_id: str) -> Optional[ContextCheckpoint]:
        candidates = [
            item for item in self._load_all()
            if item.session_id == session_id and item.turn_id == turn_id
        ]
        return max(candidates, key=lambda item: item.created_at) if candidates else None

    def latest_before(self, session_id: str, created_at: float) -> Optional[ContextCheckpoint]:
        candidates = [
            item for item in self._load_all()
            if (not session_id or item.session_id == session_id)
            and item.created_at <= float(created_at)
        ]
        return max(candidates, key=lambda item: item.created_at) if candidates else None

    def list(self, *, limit: int = 50) -> list[dict[str, object]]:
        records = sorted(
            self._load_all(), key=lambda item: item.created_at, reverse=True)
        return [item.metadata_record() for item in records[:max(1, int(limit))]]

    def _write(self, checkpoint: ContextCheckpoint) -> Optional[ContextCheckpoint]:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.dir / f"{checkpoint.id}.json"
            temporary = target.with_suffix(".json.tmp")
            line = json.dumps(
                checkpoint.as_payload(), ensure_ascii=False,
                sort_keys=True, default=str)
            if self.redactor is not None:
                line = self.redactor.redact(line)
            temporary.write_text(line + "\n", encoding="utf-8")
            temporary.replace(target)
        except (OSError, TypeError, ValueError):
            return None
        return checkpoint

    def _next_id(self, timestamp: float) -> str:
        maximum = 0
        if self.dir.is_dir():
            for path in self.dir.glob("*.json"):
                prefix = path.stem.split("-", 1)[0]
                if prefix.isdigit():
                    maximum = max(maximum, int(prefix))
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))
        return f"{maximum + 1:06d}-{stamp}-{uuid.uuid4().hex[:6]}"

    def _load_all(self) -> Iterable[ContextCheckpoint]:
        if not self.dir.is_dir():
            return []
        result = []
        for path in sorted(self.dir.glob("*.json")):
            checkpoint = self._load_file(path)
            if checkpoint is not None:
                result.append(checkpoint)
        return result

    def _load_file(self, path: Path) -> Optional[ContextCheckpoint]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("type") != "context_checkpoint":
            return None
        if raw.get("version") != CONTEXT_CHECKPOINT_VERSION:
            return None
        try:
            return ContextCheckpoint.from_payload(raw, fallback_id=path.stem)
        except (TypeError, ValueError, KeyError, AttributeError):
            return None


def restore_context_checkpoint(
    loop: CheckpointLoopPort,
    checkpoint: ContextCheckpoint,
    *,
    compact_store: Optional[CompactStorePort] = None,
    memory_store: Optional[MemoryStorePort] = None,
    history_override: Optional[Sequence[Message]] = None,
) -> ContextRestoreResult:
    """Apply one checkpoint to a loop, refreshing volatile project facts."""
    restored = copy.deepcopy(list(
        history_override if history_override is not None else checkpoint.history))
    # Keep the current base/system shell. Workspace/rules/git text is refreshed
    # from live sources before the next provider request.
    current = loop.history
    if current and current[0].role == "system":
        system = copy.deepcopy(current[0])
        if restored and restored[0].role == "system":
            restored[0] = system
        else:
            restored.insert(0, system)
    loop.update_runtime_state(history=restored)

    compact = None
    if compact_store is not None and checkpoint.compact_checkpoint_id:
        compact = compact_store.load(checkpoint.compact_checkpoint_id)
    summary = getattr(compact, "summary", None) if compact is not None else None
    if summary is None and checkpoint.compact_summary:
        try:
            from krelcode.agent.context.compact_summary import CompactSummary
            summary = CompactSummary.from_dict(checkpoint.compact_summary)
        except (TypeError, ValueError):
            summary = None
    if compact is not None:
        loop.update_runtime_state(last_compact_checkpoint=compact)
    manager = loop.context_manager
    if manager is not None and summary is not None:
        manager.restore_summary(summary)

    working_store = loop.working_context_store
    if checkpoint.working_context.get("entries"):
        snapshot = working_store.restore_snapshot(checkpoint.working_context)
        if loop.context_manager is not None:
            loop.context_manager.rebuild_state(loop.history)
    else:
        snapshot = loop.rebuild_working_context()
    integrity = checkpoint.verify_files(Path(loop.tool_context.workspace_root))
    if integrity.stale:
        loop.working_context_store.invalidate_files(
            integrity.stale, reason="resume-file-hash-mismatch")
        snapshot = loop.working_context_store.snapshot()

    relevant: Sequence[MemoryEntryPort] = ()
    memory_query = checkpoint.memory_query.strip()
    if memory_store is not None and memory_query:
        relevant = memory_store.search(memory_query, limit=8)
    rendered_memory = render_memory_context(relevant)
    loop.update_runtime_state(
        restored_memory_context=(rendered_memory,) if rendered_memory else (),
        restored_relevant_memory_ids=tuple(entry.id for entry in relevant),
        restored_context_checkpoint_id=checkpoint.id, context_integrity=integrity,
        context_workspace_fingerprint=None, restored_workspace_fingerprint=checkpoint.workspace,
    )

    plan = checkpoint.context_plan or ContextPlanSnapshot()
    previous_ledger = plan.cache_ledger
    invalidations = ["workspace-refresh"]
    if integrity.stale:
        invalidations.append("file-integrity-changed")
    restored_memory_ids = tuple(entry.id for entry in relevant)
    if (
        memory_query
        and previous_ledger.memory_evidence_ids != restored_memory_ids
    ):
        invalidations.append("memory-selection-refreshed")
    if (
        checkpoint.working_context.get("entries")
        and snapshot is not None
        and previous_ledger.working_context_sequence != snapshot.sequence
    ):
        invalidations.append("working-context-restored")
    plan = plan.restored_for(
        restored,
        checkpoint.id,
        working_context_sequence=(
            snapshot.sequence if snapshot is not None else 0),
        invalidations=invalidations,
    )
    loop.update_runtime_state(
        last_context_plan=plan,
        last_context_cache_ledger=(
            plan.cache_ledger if plan.cache_ledger.has_request_identity else None),
        previous_context_cache_ledger=None, pending_context_refresh=True,
    )

    result = ContextRestoreResult(
        checkpoint_id=checkpoint.id,
        session_id=checkpoint.session_id,
        turn_id=checkpoint.turn_id,
        history_messages=len(restored),
        compact_checkpoint_id=checkpoint.compact_checkpoint_id,
        working_context_entries=(snapshot.count if snapshot is not None else 0),
        integrity=integrity,
        memory_ids=restored_memory_ids,
        workspace_refresh_pending=True,
        context_plan=plan,
    )
    loop.update_runtime_state(last_context_restore=result)
    _refresh_now_if_possible(loop)
    loop.update_runtime_state(last_context_restore=replace(
        result,
        workspace_refresh_pending=bool(loop.pending_context_refresh),
    ))
    return cast(ContextRestoreResult, loop.last_context_restore)


def _refresh_now_if_possible(loop: CheckpointLoopPort) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(loop.refresh_workspace_context())
        except Exception:
            # The next AgentLoop turn retries; stale context is never injected.
            loop.update_runtime_state(pending_context_refresh=True)
        return
    loop.update_runtime_state(pending_context_refresh=True)


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


def workspace_fingerprint(
    workspace_root: Path,
    *,
    rules_content: str = "",
    rules_sources: Sequence[str] = (),
) -> WorkspaceFingerprint:
    root = Path(workspace_root).resolve()
    managed = root / _MANAGED_INSTRUCTION_RELPATH
    managed_digest = ""
    if managed.is_file():
        try:
            managed_digest = hashlib.sha256(managed.read_bytes()).hexdigest()
        except OSError:
            pass
    return WorkspaceFingerprint(
        branch=_git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        head=_git(root, "rev-parse", "HEAD"),
        status_digest=_digest(_git(root, "status", "--porcelain=v1")),
        tree_digest=_workspace_tree_digest(root),
        rules_digest=_digest(rules_content),
        rules_sources=tuple(str(item) for item in rules_sources),
        managed_digest=managed_digest,
        captured_at=time.time(),
    )


class ContextCheckpointCoordinator:
    """Event-bus sidecar that records start/end context checkpoints."""

    def __init__(
        self,
        *,
        loop: CheckpointLoopPort,
        store: ContextCheckpointStore,
        compact_store: Optional[CompactStorePort] = None,
        memory_store: Optional[MemoryStorePort] = None,
        redactor: Optional[SecretRedactor] = None,
        log: Optional[Callable[[str], None]] = None,
        capture_start: bool = False,
        capture_workspace: bool = True,
    ) -> None:
        self.loop = loop
        self.store = store
        self.compact_store = compact_store
        self.memory_store = memory_store
        self.redactor = redactor
        self.log = log
        self.capture_start = bool(capture_start)
        self.capture_workspace = bool(capture_workspace)
        self._unsub: Optional[Callable[[], None]] = None
        self._last_plan: dict[str, object] = {}
        self._active_session_id = ""

    def attach(self, bus: EventBus) -> None:
        if self._unsub is None:
            self._unsub = bus.subscribe(self.on_event)

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, PlanUpdated):
            self._last_plan = {
                "items": list(event.items),
                "statuses": list(event.statuses),
                "active_index": event.active_index,
                "explanation": event.explanation,
                "turn_id": event.turn_id,
            }
        elif isinstance(event, ContextAssembled):
            plan = self.loop.last_context_plan
            if plan is not None:
                self._last_plan["context_plan"] = plan.as_dict()
        elif isinstance(event, RunStarted):
            session_id = event.session_id or self._session_id()
            self._active_session_id = session_id
            if (
                self.capture_start
                and self.store.latest_for_session(session_id) is None
            ):
                history = list(self.loop.history)
                if (
                    history and history[-1].role == "user"
                    and (history[-1].content or "") == event.user_input
                ):
                    history.pop()
                await asyncio.to_thread(
                    self._capture,
                    history, session_id=session_id,
                    turn_id=event.turn_id, phase="start")
        elif isinstance(event, (RunFinished, RunCancelled)):
            history = list(self.loop.history)
            checkpoint = await asyncio.to_thread(
                self._capture,
                history,
                session_id=(
                    getattr(event, "session_id", "")
                    or self._active_session_id
                    or self._session_id()
                ),
                turn_id=event.turn_id,
                phase="cancelled" if isinstance(event, RunCancelled) else "end",
            )
            if checkpoint is not None:
                await self.loop.bus.emit(checkpoint_created_event(checkpoint))
            self._active_session_id = ""

    def _capture(self, history: Sequence[Message],
        *,
        session_id: str,
        turn_id: str,
        phase: str,
    ) -> Optional[ContextCheckpoint]:
        root = Path(self.loop.tool_context.workspace_root)
        plan = (
            cast(ContextPlanSnapshot, self.loop.last_context_plan)
            if self.loop.last_context_plan is not None
            else None
        )
        working: WorkingContextPort
        if phase == "start":
            redactor = self.loop.services.core.redactor
            scratch = WorkingContextStore(
                root,
                redactor=redactor.redact if redactor is not None else None,
            )
            working = scratch.rebuild_from_history(history)
        else:
            working = self.loop.working_context_store.snapshot()
        tracked = collect_tracked_files(root, history, working)
        file_hashes = hash_tracked_files(tracked, root)
        query = _memory_query(history)
        memory_refs = []
        if self.memory_store is not None and query:
            for entry in self.memory_store.search(query, limit=8):
                text = str(getattr(entry, "text", "") or "")
                memory_refs.append(MemoryReference(
                    id=str(getattr(entry, "id", "") or ""),
                    digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    scope=str(getattr(
                        getattr(entry, "scope", ""), "value",
                        getattr(entry, "scope", "")) or ""),
                    kind=str(getattr(
                        getattr(entry, "kind", ""), "value",
                        getattr(entry, "kind", "")) or ""),
                ))
        if self.capture_workspace:
            rule_loader = self.loop.services.context.rule_loader
            rules_content = ""
            rules_sources: list[str] = []
            if rule_loader is not None:
                try:
                    bundle = rule_loader.load_base()
                    rules_content = bundle.content
                    rules_sources = [
                        source.display_path for source in bundle.sources
                        if not source.imported
                    ]
                except Exception:
                    pass
            workspace = workspace_fingerprint(
                root, rules_content=rules_content, rules_sources=rules_sources)
        else:
            # Subagent checkpoints are scoped to their private sidechain. Their
            # parent workspace is already fingerprinted by the main session;
            # rescanning it from every child would contend on the event loop
            # and turn parallel children into serialized Git/tree scans.
            workspace = WorkspaceFingerprint(captured_at=time.time())
        compact = self.loop.last_compact_checkpoint
        compact_summary: Mapping[str, object] = {}
        if compact is not None:
            try:
                compact_summary = compact.summary.as_dict()
            except AttributeError:
                compact_summary = {}
        manager = self.loop.context_manager
        if not compact_summary and manager is not None:
            summary = getattr(manager, "last_summary", None)
            if summary is not None:
                compact_summary = summary.as_dict()
        if plan is None:
            plan = ContextPlanSnapshot()
        plan = plan.restored_for(history, str(getattr(compact, "id", "") or ""))
        checkpoint = self.store.record(
            history=history,
            session_id=session_id,
            turn_id=turn_id,
            phase=phase,
            working_context=working.as_checkpoint_record(),
            file_hashes=file_hashes,
            workspace=workspace,
            compact_checkpoint_id=str(getattr(compact, "id", "") or ""),
            compact_summary=compact_summary,
            memory_query=query,
            memory_refs=memory_refs,
            context_plan=plan,
            plan=self._last_plan,
        )
        if checkpoint is None and self.log is not None:
            self.log("[context] checkpoint 写入失败（fail-open）")
        return checkpoint

    def _session_id(self) -> str:
        runtime = self.loop.tool_context.runtime
        return str(getattr(runtime, "session_id", "") or "")


def _memory_query(history: Sequence[Message]) -> str:
    for message in reversed(history):
        if message.role != "user":
            continue
        text = " ".join(str(message.content or "").split())
        if not text or text.startswith("[上下文摘要]") or text.startswith("[用户插话]"):
            continue
        return text[:2_000]
    return ""


def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest() if value else ""


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


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            **process_isolation_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _workspace_tree_digest(root: Path) -> str:
    """Cheap directory fingerprint for stale-state invalidation evidence."""
    skip = {".git", ".krelcode", "__pycache__", "node_modules", ".venv", "venv"}
    digest = hashlib.sha256()
    count = 0
    try:
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(
                item for item in dirs
                if item not in skip and not item.startswith(".pytest")
            )
            base_path = Path(base)
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = base_path / name
                try:
                    stat = path.stat()
                    rel = path.relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                digest.update(
                    f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
                count += 1
                if count >= MAX_TREE_ENTRIES:
                    return digest.hexdigest()
    except OSError:
        pass
    return digest.hexdigest()


__all__ = [
    "CONTEXT_CHECKPOINT_DIRNAME",
    "CONTEXT_CHECKPOINT_VERSION",
    "ContextCheckpoint",
    "ContextCheckpointCoordinator",
    "ContextCheckpointStore",
    "ContextRestoreResult",
    "FileHash",
    "FileIntegrity",
    "MemoryReference",
    "WorkspaceFingerprint",
    "collect_tracked_files",
    "hash_tracked_files",
    "render_memory_context",
    "restore_context_checkpoint",
    "verify_file_hashes",
    "workspace_fingerprint",
]
