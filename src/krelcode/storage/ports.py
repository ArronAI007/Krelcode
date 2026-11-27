"""Unified local storage composition for events, state and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from .artifact_store import LocalArtifactStore
from .event_log import LocalEventLog
from .state_store import LocalTransactionalStateStore


class _RedactorPort(Protocol):
    def redact(self, text: str) -> str: ...


@dataclass(frozen=True)
class LocalStoragePorts:
    event_log: LocalEventLog
    state: LocalTransactionalStateStore
    artifacts: LocalArtifactStore

    @classmethod
    def for_workspace(
        cls,
        workspace_root: Path,
        *,
        redactor: Optional[_RedactorPort] = None,
    ) -> "LocalStoragePorts":
        root = Path(workspace_root).resolve()
        return cls(
            event_log=LocalEventLog(
                root / ".krelcode" / "events" / "events.jsonl",
                redactor=redactor,
            ),
            state=LocalTransactionalStateStore(
                root,
                redactor=redactor,
            ),
            artifacts=LocalArtifactStore(root / ".krelcode" / "artifacts"),
        )


__all__ = ["LocalStoragePorts"]
