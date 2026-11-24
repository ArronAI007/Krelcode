"""Atomic, redacted persistence for full-context checkpoints."""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from krelcode.agent.context.context_checkpoint_models import (
    CONTEXT_CHECKPOINT_DIRNAME,
    CONTEXT_CHECKPOINT_VERSION,
    ContextCheckpoint,
    FileHash,
    MemoryReference,
    WorkspaceFingerprint,
)
from krelcode.agent.context.context_plan import ContextPlanSnapshot
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor


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




__all__ = ["ContextCheckpointStore"]
