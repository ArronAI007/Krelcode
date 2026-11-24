"""Persistent pre-compact checkpoints.

A compact checkpoint is separate from file checkpoints: it preserves the
authoritative message history immediately before an LLM summary so Rewind can
recover the complete handoff site even when compaction happened between turns.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from ..agent.compact_summary import CompactSummary, summary_from_history
from ..core.messages import Message
from ..core.redact import SecretRedactor
from ..contracts.tool import ToolContext
from .codec import history_from_dicts, history_to_dicts
from .context_checkpoint import (
    FileHash,
    FileIntegrity,
    collect_tracked_files,
    hash_tracked_files,
    verify_file_hashes,
)

COMPACT_CHECKPOINT_DIRNAME = Path(".krelcode") / "compact-checkpoints"
COMPACT_CHECKPOINT_VERSION = 2
_SUPPORTED_COMPACT_CHECKPOINT_VERSIONS = {1, COMPACT_CHECKPOINT_VERSION}


class CompactCheckpointHostPort(Protocol):
    history: list[Message]
    tool_context: ToolContext

    def update_runtime_state(self, **values: object) -> None: ...


@dataclass(frozen=True)
class CompactCheckpoint:
    id: str
    created_at: float
    session_id: str
    turn_id: str
    trigger: str
    before_tokens: int
    after_tokens: int
    summary: CompactSummary
    boundary: Dict[str, object] = field(default_factory=dict)
    source_savings: Dict[str, int] = field(default_factory=dict)
    working_context: Dict[str, object] = field(default_factory=dict)
    file_hashes: Tuple[FileHash, ...] = ()
    pre_compact_history: Tuple[Message, ...] = ()

    def record(self) -> dict[str, object]:
        """Content-free metadata for events and session JSONL."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "trigger": self.trigger,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "boundary": dict(self.boundary),
            "source_savings": dict(self.source_savings),
            "file_count": len(self.file_hashes),
            "summary": self.summary.record(),
        }

    def matches_history(self, history: Sequence[Message]) -> bool:
        current = summary_from_history(history)
        return current is not None and current.digest == self.summary.digest

    def verify_files(self, workspace_root: Path) -> FileIntegrity:
        """Detect task files changed after this compact checkpoint."""
        return verify_file_hashes(self.file_hashes, Path(workspace_root))


class CompactCheckpointStore:
    """Small fail-open JSON store for compact-before histories."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.root = Path(workspace_root).resolve()
        self.dir = self.root / COMPACT_CHECKPOINT_DIRNAME
        self.redactor = redactor

    def record(
        self,
        *,
        pre_compact_history: Sequence[Message],
        summary: CompactSummary,
        trigger: str,
        before_tokens: int,
        after_tokens: int,
        session_id: str = "",
        turn_id: str = "",
        boundary: Optional[Mapping[str, object]] = None,
        source_savings: Optional[Mapping[str, int]] = None,
        working_context: Optional[Mapping[str, object]] = None,
        file_hashes: Optional[Sequence[FileHash]] = None,
        created_at: Optional[float] = None,
    ) -> Optional[CompactCheckpoint]:
        timestamp = float(created_at if created_at is not None else time.time())
        checkpoint_id = self._next_id(timestamp)
        working_record = dict(working_context or {})
        if file_hashes is None:
            entries = working_record.get("entries")
            working_view = SimpleNamespace(entries=tuple(
                SimpleNamespace(metadata=dict(item.get("metadata") or {}))
                for item in entries
                if isinstance(item, dict)
            )) if isinstance(entries, list) else None
            tracked = collect_tracked_files(
                self.root, pre_compact_history, working_view)
            captured_hashes = hash_tracked_files(tracked, self.root)
        else:
            captured_hashes = tuple(file_hashes)
        checkpoint = CompactCheckpoint(
            id=checkpoint_id,
            created_at=timestamp,
            session_id=str(session_id or ""),
            turn_id=str(turn_id or ""),
            trigger=str(trigger or "manual"),
            before_tokens=max(0, int(before_tokens)),
            after_tokens=max(0, int(after_tokens)),
            summary=summary,
            boundary=dict(boundary or {}),
            source_savings={
                str(key): int(value)
                for key, value in (source_savings or {}).items()
            },
            working_context=working_record,
            file_hashes=captured_hashes,
            pre_compact_history=tuple(pre_compact_history),
        )
        payload = {
            "type": "compact_checkpoint",
            "version": COMPACT_CHECKPOINT_VERSION,
            **checkpoint.record(),
            "summary_full": summary.as_dict(),
            "file_hashes": [item.as_dict() for item in checkpoint.file_hashes],
            "history": history_to_dicts(pre_compact_history),
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.dir / f"{checkpoint_id}.json"
            temporary = target.with_suffix(".json.tmp")
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if self.redactor is not None:
                line = self.redactor.redact(line)
            temporary.write_text(line + "\n", encoding="utf-8")
            temporary.replace(target)
        except (OSError, TypeError, ValueError):
            return None
        return checkpoint

    def find_before(
        self,
        *,
        session_id: str,
        created_at: float,
    ) -> Optional[CompactCheckpoint]:
        """Latest valid checkpoint for a session at or before a turn start."""
        candidates: List[CompactCheckpoint] = []
        for checkpoint in self._load_all():
            if session_id and checkpoint.session_id and checkpoint.session_id != session_id:
                continue
            if checkpoint.created_at <= float(created_at):
                candidates.append(checkpoint)
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.created_at)

    def find_matching_before(
        self,
        history: Sequence[Message],
        *,
        session_id: str,
        created_at: float,
    ) -> Optional[CompactCheckpoint]:
        """Find the pre-compact snapshot represented by the supplied history."""
        for checkpoint in sorted(
            self._load_all(), key=lambda item: item.created_at, reverse=True
        ):
            if session_id and checkpoint.session_id and checkpoint.session_id != session_id:
                continue
            if checkpoint.created_at > float(created_at):
                continue
            if checkpoint.matches_history(history):
                return checkpoint
        return None

    def load(self, checkpoint_id: str) -> Optional[CompactCheckpoint]:
        safe = "".join(
            char for char in str(checkpoint_id)
            if char.isalnum() or char in {"-", "_"}
        )
        if not safe:
            return None
        return self._load_file(self.dir / f"{safe}.json")

    def list(self, *, limit: int = 20) -> List[dict[str, object]]:
        checkpoints = sorted(
            self._load_all(),
            key=lambda item: item.created_at,
            reverse=True,
        )
        return [item.record() for item in checkpoints[:limit]]

    def _next_id(self, timestamp: float) -> str:
        maximum = 0
        if self.dir.is_dir():
            for path in self.dir.glob("*.json"):
                prefix = path.stem.split("-", 1)[0]
                if prefix.isdigit():
                    maximum = max(maximum, int(prefix))
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))
        return f"{maximum + 1:06d}-{stamp}-{uuid.uuid4().hex[:6]}"

    def _load_all(self) -> Iterable[CompactCheckpoint]:
        if not self.dir.is_dir():
            return []
        result = []
        for path in sorted(self.dir.glob("*.json")):
            checkpoint = self._load_file(path)
            if checkpoint is not None:
                result.append(checkpoint)
        return result

    def _load_file(self, path: Path) -> Optional[CompactCheckpoint]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if raw.get("version") not in _SUPPORTED_COMPACT_CHECKPOINT_VERSIONS:
            return None
        summary_raw = raw.get("summary_full")
        if not isinstance(summary_raw, dict):
            return None
        try:
            summary = CompactSummary.from_dict(summary_raw)
        except (TypeError, ValueError):
            return None
        messages_raw = raw.get("history")
        try:
            messages = (
                tuple(history_from_dicts(messages_raw))
                if isinstance(messages_raw, list) else ()
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            return None
        hashes_raw = raw.get("file_hashes")
        try:
            file_hashes = tuple(
                FileHash.from_dict(item)
                for item in hashes_raw
                if isinstance(item, dict)
            ) if isinstance(hashes_raw, list) else ()
        except (TypeError, ValueError):
            return None
        return CompactCheckpoint(
            id=str(raw.get("id", path.stem)),
            created_at=float(raw.get("created_at", 0.0)),
            session_id=str(raw.get("session_id", "")),
            turn_id=str(raw.get("turn_id", "")),
            trigger=str(raw.get("trigger", "manual")),
            before_tokens=int(raw.get("before_tokens", 0)),
            after_tokens=int(raw.get("after_tokens", 0)),
            summary=summary,
            boundary=dict(raw.get("boundary") or {}),
            source_savings={
                str(key): int(value)
                for key, value in dict(raw.get("source_savings") or {}).items()
            },
            working_context=dict(raw.get("working_context") or {}),
            file_hashes=file_hashes,
            pre_compact_history=messages,
        )


def restore_compact_checkpoint(
    loop: CompactCheckpointHostPort,
    checkpoint: CompactCheckpoint,
) -> FileIntegrity:
    """Restore the exact pre-compact task scene and invalidate stale files."""
    loop.update_runtime_state(last_compact_checkpoint=checkpoint)
    store = getattr(loop, "working_context_store", None)
    restore_working = getattr(store, "restore_snapshot", None)
    raw_entries = checkpoint.working_context.get("entries")
    if callable(restore_working) and isinstance(raw_entries, list) and raw_entries:
        snapshot = restore_working(checkpoint.working_context)
    else:
        rebuild = getattr(loop, "rebuild_working_context", None)
        snapshot = rebuild() if callable(rebuild) else None

    workspace_root = Path(loop.tool_context.workspace_root)
    integrity = checkpoint.verify_files(workspace_root)
    if integrity.stale and store is not None:
        store.invalidate_files(
            integrity.stale, reason="compact-file-hash-mismatch")
        snapshot = store.snapshot()
    loop.update_runtime_state(context_integrity=integrity)

    manager = getattr(loop, "context_manager", None)
    if manager is not None:
        manager.rebuild_state(loop.history)
    loop.update_runtime_state(pending_context_refresh=True)
    return integrity


__all__ = [
    "COMPACT_CHECKPOINT_DIRNAME",
    "COMPACT_CHECKPOINT_VERSION",
    "CompactCheckpoint",
    "CompactCheckpointStore",
    "restore_compact_checkpoint",
]
