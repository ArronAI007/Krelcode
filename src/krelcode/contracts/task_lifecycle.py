"""Provider-neutral contracts for connection-independent durable tasks."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Sequence

from .application import TaskBudget


class TaskErrorClass(str, Enum):
    USER_INPUT = "user_input"
    POLICY = "policy"
    PERMISSION = "permission"
    SANDBOX = "sandbox"
    PROVIDER = "provider"
    TOOL = "tool"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CONFLICT = "conflict"
    TRANSIENT = "transient"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class TaskCancelMode(str, Enum):
    COOPERATIVE = "cooperative"
    IMMEDIATE = "immediate"
    CASCADE = "cascade"


class SideEffectStatus(str, Enum):
    PENDING = "pending"
    COMMITTED = "committed"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"


@dataclass(frozen=True)
class TaskEventCursor:
    sequence: int = 0
    event_id: str = ""
    digest: str = ""
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sequence", max(0, int(self.sequence)))
        object.__setattr__(self, "event_id", str(self.event_id or ""))
        object.__setattr__(self, "digest", str(self.digest or ""))
        object.__setattr__(
            self, "updated_at", max(0.0, float(self.updated_at)))

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "digest": self.digest,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "TaskEventCursor":
        value = raw if isinstance(raw, Mapping) else {}
        return cls(
            sequence=_int(value.get("sequence"), 0),
            event_id=str(value.get("event_id") or ""),
            digest=str(value.get("digest") or ""),
            updated_at=_float(value.get("updated_at"), 0.0),
        )


@dataclass(frozen=True)
class TaskErrorInfo:
    category: TaskErrorClass | str = TaskErrorClass.UNKNOWN
    retryable: bool = False
    message: str = ""
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", TaskErrorClass(self.category))
        object.__setattr__(self, "retryable", bool(self.retryable))
        object.__setattr__(self, "message", str(self.message or ""))
        object.__setattr__(
            self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> dict[str, object]:
        return {
            "category": TaskErrorClass(self.category).value,
            "retryable": self.retryable,
            "message": self.message,
            "details": dict(self.details),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "TaskErrorInfo":
        value = raw if isinstance(raw, Mapping) else {}
        details = value.get("details")
        return cls(
            category=str(value.get("category") or "unknown"),
            retryable=bool(value.get("retryable", False)),
            message=str(value.get("message") or ""),
            details=(
                dict(details) if isinstance(details, Mapping) else {}),
        )


@dataclass(frozen=True)
class TaskResultLocation:
    kind: str = ""
    uri: str = ""
    artifact_ids: tuple[str, ...] = ()
    digest: str = ""
    summary: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind or ""))
        object.__setattr__(self, "uri", str(self.uri or ""))
        object.__setattr__(
            self, "artifact_ids", _strings(self.artifact_ids))
        object.__setattr__(self, "digest", str(self.digest or ""))
        object.__setattr__(self, "summary", str(self.summary or ""))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def available(self) -> bool:
        return bool(self.uri or self.artifact_ids or self.summary)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "uri": self.uri,
            "artifact_ids": list(self.artifact_ids),
            "digest": self.digest,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "TaskResultLocation":
        value = raw if isinstance(raw, Mapping) else {}
        metadata = value.get("metadata")
        return cls(
            kind=str(value.get("kind") or ""),
            uri=str(value.get("uri") or ""),
            artifact_ids=_strings(value.get("artifact_ids")),
            digest=str(value.get("digest") or ""),
            summary=str(value.get("summary") or ""),
            metadata=(
                dict(metadata) if isinstance(metadata, Mapping) else {}),
        )


@dataclass(frozen=True)
class TaskContinuation:
    objective: str
    plan: Mapping[str, object] = field(default_factory=dict)
    confirmed_facts: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    artifacts: tuple[Mapping[str, object], ...] = ()
    waiting_items: tuple[str, ...] = ()
    remaining_budget: TaskBudget = field(default_factory=TaskBudget)
    permission_mode: str = ""
    workspace_version: Mapping[str, object] = field(default_factory=dict)
    recovery_checkpoint_id: str = ""
    instruction_ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        objective = " ".join(str(self.objective or "").split())
        if objective:
            object.__setattr__(self, "objective", objective)
        object.__setattr__(self, "plan", MappingProxyType(dict(self.plan)))
        for name in (
            "confirmed_facts", "failed_attempts", "waiting_items",
            "instruction_ids",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name)))
        object.__setattr__(
            self,
            "artifacts",
            tuple(
                MappingProxyType(dict(item))
                for item in self.artifacts
                if isinstance(item, Mapping)
            ),
        )
        object.__setattr__(
            self,
            "workspace_version",
            MappingProxyType(dict(self.workspace_version)),
        )
        object.__setattr__(
            self, "recovery_checkpoint_id",
            str(self.recovery_checkpoint_id or "").strip())
        object.__setattr__(
            self, "permission_mode", str(self.permission_mode or ""))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "plan": dict(self.plan),
            "confirmed_facts": list(self.confirmed_facts),
            "failed_attempts": list(self.failed_attempts),
            "artifacts": [dict(item) for item in self.artifacts],
            "waiting_items": list(self.waiting_items),
            "remaining_budget": self.remaining_budget.to_dict(),
            "permission_mode": self.permission_mode,
            "workspace_version": dict(self.workspace_version),
            "recovery_checkpoint_id": self.recovery_checkpoint_id,
            "instruction_ids": list(self.instruction_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "TaskContinuation":
        value = raw if isinstance(raw, Mapping) else {}
        budget = value.get("remaining_budget")
        workspace = value.get("workspace_version")
        artifacts = value.get("artifacts")
        return cls(
            objective=str(value.get("objective") or ""),
            plan=_mapping(value.get("plan")),
            confirmed_facts=_strings(value.get("confirmed_facts")),
            failed_attempts=_strings(value.get("failed_attempts")),
            artifacts=tuple(
                dict(item) for item in (
                    artifacts if isinstance(artifacts, list) else ())
                if isinstance(item, Mapping)
            ),
            waiting_items=_strings(value.get("waiting_items")),
            remaining_budget=TaskBudget.from_mapping(
                budget if isinstance(budget, Mapping) else {}),
            permission_mode=str(value.get("permission_mode") or ""),
            workspace_version=(
                dict(workspace) if isinstance(workspace, Mapping) else {}),
            recovery_checkpoint_id=str(
                value.get("recovery_checkpoint_id") or ""),
            instruction_ids=_strings(value.get("instruction_ids")),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class TaskSideEffectRecord:
    effect_id: str
    task_id: str
    kind: str
    resource: str
    status: SideEffectStatus | str = SideEffectStatus.COMMITTED
    recorded_at: float = field(default_factory=time.time)
    compensation: str = ""
    evidence: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("effect_id", "task_id", "kind", "resource", "compensation"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.effect_id:
            object.__setattr__(
                self,
                "effect_id",
                "effect-" + hashlib.sha256(
                    f"{self.task_id}:{self.kind}:{self.resource}:"
                    f"{time.time_ns()}".encode("utf-8")
                ).hexdigest()[:16],
            )
        object.__setattr__(self, "status", SideEffectStatus(self.status))
        object.__setattr__(
            self, "recorded_at", max(0.0, float(self.recorded_at)))
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "resource": self.resource,
            "status": SideEffectStatus(self.status).value,
            "recorded_at": self.recorded_at,
            "compensation": self.compensation,
            "evidence": dict(self.evidence),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "TaskSideEffectRecord":
        value = raw if isinstance(raw, Mapping) else {}
        return cls(
            effect_id=str(value.get("effect_id") or ""),
            task_id=str(value.get("task_id") or ""),
            kind=str(value.get("kind") or ""),
            resource=str(value.get("resource") or ""),
            status=str(value.get("status") or "committed"),
            recorded_at=_float(value.get("recorded_at"), time.time()),
            compensation=str(value.get("compensation") or ""),
            evidence=_mapping(value.get("evidence")),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class TaskRecoveryCheck:
    code: str
    passed: bool
    reason: str = ""
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", str(self.code or "unknown"))
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "passed": self.passed,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class TaskRecoveryDecision:
    allowed: bool
    checks: tuple[TaskRecoveryCheck, ...] = ()
    requires_user: bool = False
    continuation: Optional[TaskContinuation] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "requires_user", bool(self.requires_user))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def failed_checks(self) -> tuple[TaskRecoveryCheck, ...]:
        return tuple(item for item in self.checks if not item.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "checks": [item.to_dict() for item in self.checks],
            "requires_user": self.requires_user,
            "continuation": (
                self.continuation.to_dict()
                if self.continuation is not None else None),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskAttachment:
    task_id: str
    cursor: TaskEventCursor
    status: str
    result: Optional[TaskResultLocation] = None
    error: Optional[TaskErrorInfo] = None
    continuation: Optional[TaskContinuation] = None
    side_effects: tuple[TaskSideEffectRecord, ...] = ()
    recovery_decision: Optional[TaskRecoveryDecision] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", str(self.task_id or ""))
        object.__setattr__(self, "status", str(self.status or ""))
        object.__setattr__(self, "side_effects", tuple(self.side_effects))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "cursor": self.cursor.to_dict(),
            "status": self.status,
            "result": self.result.to_dict() if self.result is not None else None,
            "error": self.error.to_dict() if self.error is not None else None,
            "continuation": (
                self.continuation.to_dict()
                if self.continuation is not None else None),
            "side_effects": [
                item.to_dict() for item in self.side_effects
            ],
            "recovery_decision": (
                self.recovery_decision.to_dict()
                if self.recovery_decision is not None else None),
            "metadata": dict(self.metadata),
        }


class TaskRecoveryVerifierPort(Protocol):
    async def verify(
        self,
        task: object,
        state: object,
        continuation: Optional[TaskContinuation],
    ) -> TaskRecoveryDecision: ...


class TaskEventJournalPort(Protocol):
    def append(
        self,
        task_id: str,
        payload: Mapping[str, object],
        *,
        event_type: str = "",
        parent_task_id: str = "",
    ) -> TaskEventCursor: ...

    def read(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> Sequence[Mapping[str, object]]: ...

    def cursor(self, task_id: str) -> TaskEventCursor: ...


def classify_task_error(error: BaseException | str) -> TaskErrorInfo:
    if isinstance(error, BaseException):
        name = type(error).__name__
        message = f"{name}: {error}"
    else:
        name = ""
        message = str(error or "")
    lowered = f"{name} {message}".lower()
    if "cancel" in lowered:
        category = TaskErrorClass.CANCELLED
    elif "timeout" in lowered or "deadline" in lowered:
        category = TaskErrorClass.TIMEOUT
    elif "permission" in lowered or "approval" in lowered:
        category = TaskErrorClass.PERMISSION
    elif "sandbox" in lowered:
        category = TaskErrorClass.SANDBOX
    elif "policy" in lowered or "denied" in lowered:
        category = TaskErrorClass.POLICY
    elif "provider" in lowered or "model" in lowered:
        category = TaskErrorClass.PROVIDER
    elif "tool" in lowered:
        category = TaskErrorClass.TOOL
    elif "conflict" in lowered or "idempot" in lowered:
        category = TaskErrorClass.CONFLICT
    elif "transient" in lowered or "retry" in lowered:
        category = TaskErrorClass.TRANSIENT
    elif "input" in lowered:
        category = TaskErrorClass.USER_INPUT
    else:
        category = TaskErrorClass.UNKNOWN
    return TaskErrorInfo(
        category=category,
        retryable=category in {
            TaskErrorClass.TIMEOUT,
            TaskErrorClass.PROVIDER,
            TaskErrorClass.TRANSIENT,
        },
        message=message,
    )


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        values = value
    else:
        values = ()
    return tuple(dict.fromkeys(
        text for item in values
        if (text := str(item).strip())
    ))


def _int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "SideEffectStatus",
    "TaskAttachment",
    "TaskCancelMode",
    "TaskContinuation",
    "TaskErrorClass",
    "TaskErrorInfo",
    "TaskEventCursor",
    "TaskEventJournalPort",
    "TaskRecoveryCheck",
    "TaskRecoveryDecision",
    "TaskRecoveryVerifierPort",
    "TaskResultLocation",
    "TaskSideEffectRecord",
    "classify_task_error",
]
