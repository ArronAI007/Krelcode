"""Unified, transactionally committable task/session state snapshots.

``StateCheckpointBundle`` remains the compatibility DTO name, but it now carries
the actual semantic state components that belong to one recoverable point.
Large external payloads may still be represented by opaque references inside a
component, however the commit boundary itself is no longer just a collection of
independent references.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Protocol

from .runtime import (
    RuntimeCheckpointRef,
    RuntimeLease,
    RuntimeStateRef,
    RuntimeTaskState,
    RuntimeTaskStatus,
)


class StateSnapshotStatus(str, Enum):
    DRAFT = "draft"
    STAGED = "staged"
    COMMITTED = "committed"


@dataclass(frozen=True)
class StateCheckpointBundle:
    """One coherent, committable state snapshot.

    ``components`` is the authoritative semantic state.  The legacy fields are
    retained as projections for older readers and as a migration fallback.
    """

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
    event_sequence: int = 0
    event_digest: str = ""
    components: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    component_digests: Mapping[str, str] = field(default_factory=dict)
    transaction_id: str = ""
    snapshot_digest: str = ""
    status: StateSnapshotStatus | str = StateSnapshotStatus.DRAFT
    sequence: int = 0
    parent_snapshot_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bundle_id", str(self.bundle_id or "").strip())
        if not self.bundle_id:
            raise ValueError("state bundle_id 不能为空")
        object.__setattr__(
            self, "session_id", str(self.session_id or "").strip())
        object.__setattr__(self, "turn_id", str(self.turn_id or "").strip())
        object.__setattr__(self, "task_id", str(self.task_id or "").strip())
        object.__setattr__(
            self,
            "context_checkpoint_id",
            str(self.context_checkpoint_id or "").strip(),
        )
        object.__setattr__(
            self,
            "compact_checkpoint_id",
            str(self.compact_checkpoint_id or "").strip(),
        )
        object.__setattr__(
            self,
            "working_context_sequence",
            max(0, int(self.working_context_sequence)),
        )
        object.__setattr__(
            self, "history_digest", str(self.history_digest or "").strip())
        object.__setattr__(
            self, "event_sequence", max(0, int(self.event_sequence)))
        object.__setattr__(
            self, "event_digest", str(self.event_digest or "").strip())
        object.__setattr__(self, "plan", MappingProxyType(dict(self.plan)))
        object.__setattr__(self, "todos", MappingProxyType(dict(self.todos)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(
            self,
            "components",
            MappingProxyType({
                str(name): MappingProxyType(dict(data))
                for name, data in self.components.items()
                if str(name).strip() and isinstance(data, Mapping)
            }),
        )
        object.__setattr__(
            self,
            "status",
            StateSnapshotStatus(self.status),
        )
        object.__setattr__(self, "sequence", max(0, int(self.sequence)))
        object.__setattr__(
            self,
            "transaction_id",
            str(self.transaction_id or "").strip(),
        )
        object.__setattr__(
            self,
            "parent_snapshot_id",
            str(self.parent_snapshot_id or "").strip(),
        )
        effective = self.effective_components()
        computed_digests = {
            name: component_digest(data)
            for name, data in effective.items()
        }
        supplied_digests = {
            str(name): str(digest)
            for name, digest in self.component_digests.items()
        }
        for name, digest in supplied_digests.items():
            if name not in computed_digests:
                raise ValueError(
                    f"component_digests 包含未知组件: {name}")
            if digest and digest != computed_digests[name]:
                raise ValueError(
                    f"状态组件摘要不匹配: {name}")
        object.__setattr__(
            self,
            "component_digests",
            MappingProxyType(computed_digests),
        )
        computed_snapshot_digest = self._compute_snapshot_digest(effective)
        if self.snapshot_digest and (
            self.snapshot_digest != computed_snapshot_digest
        ):
            raise ValueError("state snapshot_digest 不匹配")
        object.__setattr__(
            self,
            "snapshot_digest",
            computed_snapshot_digest,
        )

    def effective_components(self) -> dict[str, dict[str, object]]:
        """Return explicit components plus legacy-field projections."""
        projected: dict[str, dict[str, object]] = {
            "legacy": {
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
            },
            "refs": {
                "context_checkpoint_id": self.context_checkpoint_id,
                "compact_checkpoint_id": self.compact_checkpoint_id,
                "working_context_sequence": self.working_context_sequence,
                "history_digest": self.history_digest,
                "event_sequence": self.event_sequence,
                "event_digest": self.event_digest,
            },
        }
        for name, data in self.components.items():
            projected[name] = dict(data)
        return projected

    def with_commit(
        self,
        *,
        transaction_id: str,
        sequence: int,
        parent_snapshot_id: str = "",
    ) -> "StateCheckpointBundle":
        return StateCheckpointBundle(
            bundle_id=self.bundle_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            task_id=self.task_id,
            context_checkpoint_id=self.context_checkpoint_id,
            compact_checkpoint_id=self.compact_checkpoint_id,
            working_context_sequence=self.working_context_sequence,
            history_digest=self.history_digest,
            event_sequence=self.event_sequence,
            event_digest=self.event_digest,
            plan=self.plan,
            todos=self.todos,
            runtime_state=self.runtime_state,
            state_ref=self.state_ref,
            created_at=self.created_at,
            metadata=self.metadata,
            components=self.components,
            component_digests=self.component_digests,
            transaction_id=transaction_id,
            snapshot_digest=self.snapshot_digest,
            status=StateSnapshotStatus.COMMITTED,
            sequence=sequence,
            parent_snapshot_id=parent_snapshot_id,
        )

    def _compute_snapshot_digest(
        self,
        components: Mapping[str, Mapping[str, object]],
    ) -> str:
        payload = {
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "components": components,
        }
        return canonical_digest(payload)

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
            "event_sequence": self.event_sequence,
            "event_digest": self.event_digest,
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
            "components": {
                name: dict(data)
                for name, data in self.components.items()
            },
            "component_digests": dict(self.component_digests),
            "transaction_id": self.transaction_id,
            "snapshot_digest": self.snapshot_digest,
            "status": StateSnapshotStatus(self.status).value,
            "sequence": self.sequence,
            "parent_snapshot_id": self.parent_snapshot_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "StateCheckpointBundle":
        components_raw = raw.get("components")
        components: dict[str, Mapping[str, object]] = {}
        if isinstance(components_raw, Mapping):
            for name, value in components_raw.items():
                if isinstance(value, Mapping):
                    components[str(name)] = dict(value)
        digests_raw = raw.get("component_digests")
        digests = {
            str(name): str(digest)
            for name, digest in (
                digests_raw.items()
                if isinstance(digests_raw, Mapping) else ()
            )
        }
        return cls(
            bundle_id=str(raw.get("bundle_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            turn_id=str(raw.get("turn_id") or ""),
            task_id=str(raw.get("task_id") or ""),
            context_checkpoint_id=str(
                raw.get("context_checkpoint_id") or ""),
            compact_checkpoint_id=str(
                raw.get("compact_checkpoint_id") or ""),
            working_context_sequence=_int_value(
                raw.get("working_context_sequence"), 0),
            history_digest=str(raw.get("history_digest") or ""),
            event_sequence=_int_value(raw.get("event_sequence"), 0),
            event_digest=str(raw.get("event_digest") or ""),
            plan=_mapping(raw.get("plan")),
            todos=_mapping(raw.get("todos")),
            runtime_state=_task_state_from_mapping(
                raw.get("runtime_state")),
            state_ref=_state_ref_from_mapping(raw.get("state_ref")),
            created_at=_float_value(raw.get("created_at"), 0.0),
            metadata=_mapping(raw.get("metadata")),
            components=components,
            component_digests=digests,
            transaction_id=str(raw.get("transaction_id") or ""),
            snapshot_digest=str(raw.get("snapshot_digest") or ""),
            status=str(
                raw.get("status") or StateSnapshotStatus.DRAFT.value),
            sequence=_int_value(raw.get("sequence"), 0),
            parent_snapshot_id=str(raw.get("parent_snapshot_id") or ""),
        )


def component_digest(component: Mapping[str, object]) -> str:
    return canonical_digest(dict(component))


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        "attempts": state.attempts,
        "deadline_at": state.deadline_at,
        "started_at": state.started_at,
        "paused_at": state.paused_at,
        "resume_from_checkpoint_id": state.resume_from_checkpoint_id,
        "updated_at": state.updated_at,
        "wait_reason": state.wait_reason,
        "wait_kind": state.wait_kind,
        "resume_token": state.resume_token,
        "waiting_since": state.waiting_since,
        "resumed_at": state.resumed_at,
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


def _mapping(raw: object) -> dict[str, object]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _optional_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _int_value(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _float_value(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _task_state_from_mapping(raw: object) -> Optional[RuntimeTaskState]:
    if not isinstance(raw, Mapping):
        return None
    lease = None
    lease_raw = raw.get("lease")
    if isinstance(lease_raw, Mapping):
        lease = RuntimeLease(
            lease_id=str(lease_raw.get("lease_id") or ""),
            task_id=str(lease_raw.get("task_id") or ""),
            owner=str(lease_raw.get("owner") or ""),
            acquired_at=_float_value(lease_raw.get("acquired_at"), 0.0),
            expires_at=_float_value(lease_raw.get("expires_at"), 0.0),
        )
    return RuntimeTaskState(
        task_id=str(raw.get("task_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        status=RuntimeTaskStatus(str(raw.get("status") or "interrupted")),
        lease=lease,
        checkpoint=_checkpoint_from_mapping(raw.get("checkpoint")),
        sequence=_int_value(raw.get("sequence"), 0),
        error=str(raw.get("error") or ""),
        attempts=_int_value(raw.get("attempts"), 0),
        deadline_at=_optional_float(raw.get("deadline_at")),
        started_at=_optional_float(raw.get("started_at")),
        paused_at=_optional_float(raw.get("paused_at")),
        resume_from_checkpoint_id=str(
            raw.get("resume_from_checkpoint_id") or ""),
        updated_at=_float_value(raw.get("updated_at"), 0.0),
        wait_reason=str(raw.get("wait_reason") or ""),
        wait_kind=str(raw.get("wait_kind") or ""),
        resume_token=str(raw.get("resume_token") or ""),
        waiting_since=_optional_float(raw.get("waiting_since")),
        resumed_at=_optional_float(raw.get("resumed_at")),
    )


def _state_ref_from_mapping(raw: object) -> Optional[RuntimeStateRef]:
    if not isinstance(raw, Mapping):
        return None
    return RuntimeStateRef(
        state_id=str(raw.get("state_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        sequence=_int_value(raw.get("sequence"), 0),
        updated_at=_float_value(raw.get("updated_at"), 0.0),
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


class StateCheckpointStorePort(Protocol):
    def commit(self, bundle: StateCheckpointBundle) -> StateCheckpointBundle: ...

    def save(self, bundle: StateCheckpointBundle) -> bool: ...

    def load(self, bundle_id: str) -> Optional[StateCheckpointBundle]: ...

    def latest_for_session(
        self,
        session_id: str,
    ) -> Optional[StateCheckpointBundle]: ...


StateStorePort = StateCheckpointStorePort
StateSnapshot = StateCheckpointBundle
StateSnapshotStorePort = StateStorePort


__all__ = [
    "StateCheckpointBundle",
    "StateCheckpointStorePort",
    "StateSnapshotStatus",
    "StateSnapshot",
    "StateSnapshotStorePort",
    "StateStorePort",
    "canonical_digest",
    "component_digest",
]
