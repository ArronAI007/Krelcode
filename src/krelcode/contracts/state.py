"""Unified task/session state checkpoint references."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Protocol

from .runtime import (
    RuntimeCheckpointRef,
    RuntimeLease,
    RuntimeStateRef,
    RuntimeTaskState,
)


@dataclass(frozen=True)
class StateCheckpointBundle:
    """One coherent handoff bundle assembled from existing state stores."""

    bundle_id: str
    session_id: str = ""
    turn_id: str = ""
    task_id: str = ""
    context_checkpoint_id: str = ""
    compact_checkpoint_id: str = ""
    working_context_sequence: int = 0
    history_digest: str = ""
    plan: Mapping[str, object] = field(default_factory=dict)
    todos: Mapping[str, object] = field(default_factory=dict)
    runtime_state: Optional[RuntimeTaskState] = None
    state_ref: Optional[RuntimeStateRef] = None
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_id", str(self.bundle_id or "").strip())
        if not self.bundle_id:
            raise ValueError("state bundle_id 不能为空")
        object.__setattr__(self, "plan", MappingProxyType(dict(self.plan)))
        object.__setattr__(self, "todos", MappingProxyType(dict(self.todos)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_dict(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "context_checkpoint_id": self.context_checkpoint_id,
            "compact_checkpoint_id": self.compact_checkpoint_id,
            "working_context_sequence": self.working_context_sequence,
            "history_digest": self.history_digest,
            "plan": dict(self.plan),
            "todos": dict(self.todos),
            "runtime_state": (
                _task_state_to_dict(self.runtime_state)
                if self.runtime_state is not None else None
            ),
            "state_ref": (
                _state_ref_to_dict(self.state_ref)
                if self.state_ref is not None else None
            ),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


def _checkpoint_to_dict(checkpoint: RuntimeCheckpointRef) -> dict[str, object]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "kind": checkpoint.kind,
        "session_id": checkpoint.session_id,
        "turn_id": checkpoint.turn_id,
        "task_id": checkpoint.task_id,
        "digest": checkpoint.digest,
        "path": checkpoint.path,
        "metadata": dict(checkpoint.metadata),
    }


def _task_state_to_dict(state: RuntimeTaskState) -> dict[str, object]:
    lease = state.lease
    return {
        "task_id": state.task_id,
        "turn_id": state.turn_id,
        "status": state.status.value,
        "lease": (
            {
                "lease_id": lease.lease_id,
                "task_id": lease.task_id,
                "owner": lease.owner,
                "acquired_at": lease.acquired_at,
                "expires_at": lease.expires_at,
            }
            if isinstance(lease, RuntimeLease) else None
        ),
        "checkpoint": (
            _checkpoint_to_dict(state.checkpoint)
            if state.checkpoint is not None else None
        ),
        "sequence": state.sequence,
        "error": state.error,
        "updated_at": state.updated_at,
    }


def _state_ref_to_dict(state_ref: RuntimeStateRef) -> dict[str, object]:
    return {
        "state_id": state_ref.state_id,
        "task_id": state_ref.task_id,
        "turn_id": state_ref.turn_id,
        "sequence": state_ref.sequence,
        "updated_at": state_ref.updated_at,
        "checkpoint": (
            _checkpoint_to_dict(state_ref.checkpoint)
            if state_ref.checkpoint is not None else None
        ),
    }


class StateCheckpointStorePort(Protocol):
    def save(self, bundle: StateCheckpointBundle) -> bool: ...

    def load(self, bundle_id: str) -> Optional[StateCheckpointBundle]: ...

    def latest_for_session(
        self,
        session_id: str,
    ) -> Optional[StateCheckpointBundle]: ...


__all__ = ["StateCheckpointBundle", "StateCheckpointStorePort"]
