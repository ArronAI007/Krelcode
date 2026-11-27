"""Persistent content-light StateCheckpointBundle store."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Optional, Protocol

from ..contracts.runtime import (
    RuntimeCheckpointRef,
    RuntimeLease,
    RuntimeStateRef,
    RuntimeTaskState,
    RuntimeTaskStatus,
)
from ..contracts.state import StateCheckpointBundle, StateCheckpointStorePort
STATE_CHECKPOINT_VERSION = 1


class _RedactorPort(Protocol):
    def redact(self, text: str) -> str: ...


class LocalStateCheckpointStore(StateCheckpointStorePort):
    """Atomic JSON store for unified state checkpoint bundles."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        directory: Optional[Path] = None,
        redactor: Optional[_RedactorPort] = None,
    ) -> None:
        self.root = Path(workspace_root).resolve()
        self.dir = (
            Path(directory)
            if directory is not None
            else self.root / ".krelcode" / "state" / "checkpoints"
        )
        self.redactor = redactor
        self.last_error = ""

    def save(self, bundle: StateCheckpointBundle) -> bool:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            safe = _safe_id(bundle.bundle_id)
            target = self.dir / f"{safe}.json"
            temporary = target.with_suffix(".json.tmp")
            line = json.dumps(
                {
                    "version": STATE_CHECKPOINT_VERSION,
                    "type": "state_checkpoint_bundle",
                    **bundle.as_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if self.redactor is not None:
                line = self.redactor.redact(line)
            temporary.write_text(line + "\n", encoding="utf-8")
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            temporary.replace(target)
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = ""
        return True

    def load(self, bundle_id: str) -> Optional[StateCheckpointBundle]:
        return self._load_path(self.dir / f"{_safe_id(bundle_id)}.json")

    def latest_for_session(
        self,
        session_id: str,
    ) -> Optional[StateCheckpointBundle]:
        if not self.dir.is_dir():
            return None
        matches: list[StateCheckpointBundle] = []
        for path in sorted(self.dir.glob("*.json")):
            bundle = self._load_path(path)
            if bundle is not None and (
                    not session_id or bundle.session_id == session_id):
                matches.append(bundle)
        return max(matches, key=lambda item: item.created_at) if matches else None

    def _load_path(self, path: Path) -> Optional[StateCheckpointBundle]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping):
            return None
        if raw.get("version") != STATE_CHECKPOINT_VERSION:
            return None
        if raw.get("type") != "state_checkpoint_bundle":
            return None
        try:
            return StateCheckpointBundle(
                bundle_id=str(raw.get("bundle_id") or ""),
                session_id=str(raw.get("session_id") or ""),
                turn_id=str(raw.get("turn_id") or ""),
                task_id=str(raw.get("task_id") or ""),
                context_checkpoint_id=str(
                    raw.get("context_checkpoint_id") or ""),
                compact_checkpoint_id=str(
                    raw.get("compact_checkpoint_id") or ""),
                working_context_sequence=int(
                    raw.get("working_context_sequence") or 0),
                history_digest=str(raw.get("history_digest") or ""),
                plan=_mapping(raw.get("plan")),
                todos=_mapping(raw.get("todos")),
                runtime_state=_task_state_from_mapping(
                    raw.get("runtime_state")),
                state_ref=_state_ref_from_mapping(raw.get("state_ref")),
                created_at=float(raw.get("created_at") or 0.0),
                metadata=_mapping(raw.get("metadata")),
            )
        except (TypeError, ValueError, KeyError):
            return None


def _task_state_from_mapping(raw: object) -> Optional[RuntimeTaskState]:
    if not isinstance(raw, Mapping):
        return None
    lease_raw = raw.get("lease")
    lease = None
    if isinstance(lease_raw, Mapping):
        lease = RuntimeLease(
            lease_id=str(lease_raw.get("lease_id") or ""),
            task_id=str(lease_raw.get("task_id") or ""),
            owner=str(lease_raw.get("owner") or ""),
            acquired_at=float(lease_raw.get("acquired_at") or 0.0),
            expires_at=float(lease_raw.get("expires_at") or 0.0),
        )
    return RuntimeTaskState(
        task_id=str(raw.get("task_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        status=RuntimeTaskStatus(str(raw.get("status") or "interrupted")),
        lease=lease,
        checkpoint=_checkpoint_from_mapping(raw.get("checkpoint")),
        sequence=int(raw.get("sequence") or 0),
        error=str(raw.get("error") or ""),
        updated_at=float(raw.get("updated_at") or 0.0),
    )


def _state_ref_from_mapping(raw: object) -> Optional[RuntimeStateRef]:
    if not isinstance(raw, Mapping):
        return None
    return RuntimeStateRef(
        state_id=str(raw.get("state_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        sequence=int(raw.get("sequence") or 0),
        updated_at=float(raw.get("updated_at") or 0.0),
        checkpoint=_checkpoint_from_mapping(raw.get("checkpoint")),
    )


def _checkpoint_from_mapping(raw: object) -> Optional[RuntimeCheckpointRef]:
    if not isinstance(raw, Mapping):
        return None
    return RuntimeCheckpointRef(
        checkpoint_id=str(raw.get("checkpoint_id") or ""),
        kind=str(raw.get("kind") or "context"),
        session_id=str(raw.get("session_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        digest=str(raw.get("digest") or ""),
        path=str(raw.get("path") or ""),
        metadata=_mapping(raw.get("metadata")),
    )


def _mapping(raw: object) -> dict[str, object]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _safe_id(value: str) -> str:
    safe = "".join(
        char for char in str(value)
        if char.isalnum() or char in {"-", "_", "."}
    )
    return safe or "state-checkpoint"


__all__ = ["LocalStateCheckpointStore", "STATE_CHECKPOINT_VERSION"]
