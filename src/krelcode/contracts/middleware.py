"""Structured middleware and ability contracts for the stable Agent Loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence


class ExtensionLifecyclePoint(str, Enum):
    TASK_START = "task_start"
    PREPARE_BEFORE = "prepare_before"
    PREPARE_AFTER = "prepare_after"
    MODEL_BEFORE = "model_before"
    MODEL_AFTER = "model_after"
    ACTION_BEFORE = "action_before"
    ACTION_AFTER = "action_after"
    OBSERVE_BEFORE = "observe_before"
    OBSERVE_AFTER = "observe_after"
    VERIFY_BEFORE = "verify_before"
    VERIFY_AFTER = "verify_after"
    TASK_COMPLETE = "task_complete"
    TASK_FAIL = "task_fail"
    STATE_COMMIT = "state_commit"


class MiddlewareAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    SHORT_CIRCUIT = "short_circuit"
    PATCH = "patch"


class MiddlewareFailurePolicy(str, Enum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"
    WARN = "warn"


class PatchOperation(str, Enum):
    APPEND = "append"
    SET = "set"
    REMOVE = "remove"


class PatchScope(str, Enum):
    RUNTIME_CONTEXT = "runtime_context"
    STATE_METADATA = "state_metadata"
    OBSERVATION = "observation"
    PLAN = "plan"


@dataclass(frozen=True)
class AbilityManifest:
    id: str
    version: str = "1"
    lifecycle_points: tuple[ExtensionLifecyclePoint, ...] = ()
    priority: int = 0
    depends_on: tuple[str, ...] = ()
    read_scopes: tuple[str, ...] = ()
    write_scopes: tuple[PatchScope, ...] = ()
    failure_policy: MiddlewareFailurePolicy | str = (
        MiddlewareFailurePolicy.FAIL_CLOSED)
    replay_safe: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        if not self.id:
            raise ValueError("AbilityManifest.id 不能为空")
        object.__setattr__(self, "version", str(self.version or "1"))
        object.__setattr__(
            self,
            "lifecycle_points",
            tuple(ExtensionLifecyclePoint(item)
                  for item in self.lifecycle_points),
        )
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "depends_on", _strings(self.depends_on))
        object.__setattr__(self, "read_scopes", _strings(self.read_scopes))
        object.__setattr__(
            self,
            "write_scopes",
            tuple(PatchScope(item) for item in self.write_scopes),
        )
        object.__setattr__(
            self, "failure_policy",
            MiddlewareFailurePolicy(self.failure_policy))
        object.__setattr__(self, "replay_safe", bool(self.replay_safe))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "lifecycle_points": [
                item.value for item in self.lifecycle_points
            ],
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "read_scopes": list(self.read_scopes),
            "write_scopes": [item.value for item in self.write_scopes],
            "failure_policy": MiddlewareFailurePolicy(
                self.failure_policy).value,
            "replay_safe": self.replay_safe,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class StatePatch:
    scope: PatchScope | str
    operation: PatchOperation | str
    path: str
    value: object = None
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", PatchScope(self.scope))
        object.__setattr__(self, "operation", PatchOperation(self.operation))
        object.__setattr__(self, "path", str(self.path or "").strip())
        object.__setattr__(self, "reason", str(self.reason or ""))
        if self.scope is not PatchScope.RUNTIME_CONTEXT and not self.path:
            raise ValueError("非 runtime_context Patch 必须提供 path")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": PatchScope(self.scope).value,
            "operation": PatchOperation(self.operation).value,
            "path": self.path,
            "value": self.value,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MiddlewareContext:
    turn_id: str
    phase: str
    step: int
    lifecycle_point: ExtensionLifecyclePoint | str
    goal: str = ""
    permission_mode: str = ""
    plan: Mapping[str, object] = field(default_factory=dict)
    budget: Mapping[str, object] = field(default_factory=dict)
    observation_ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "turn_id", str(self.turn_id or ""))
        object.__setattr__(self, "phase", str(self.phase or ""))
        object.__setattr__(self, "step", max(0, int(self.step)))
        object.__setattr__(
            self,
            "lifecycle_point",
            ExtensionLifecyclePoint(self.lifecycle_point),
        )
        object.__setattr__(self, "goal", str(self.goal or ""))
        object.__setattr__(
            self, "permission_mode", str(self.permission_mode or ""))
        object.__setattr__(self, "plan", MappingProxyType(dict(self.plan)))
        object.__setattr__(self, "budget", MappingProxyType(dict(self.budget)))
        object.__setattr__(
            self, "observation_ids", _strings(self.observation_ids))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class MiddlewareDecision:
    action: MiddlewareAction | str = MiddlewareAction.ALLOW
    patches: tuple[StatePatch, ...] = ()
    reason: str = ""
    short_circuit_text: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", MiddlewareAction(self.action))
        object.__setattr__(self, "patches", tuple(self.patches))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(
            self, "short_circuit_text", str(self.short_circuit_text or ""))
        if (
            self.action is MiddlewareAction.SHORT_CIRCUIT
            and not self.short_circuit_text
        ):
            raise ValueError("SHORT_CIRCUIT decision 必须提供文本")
        if self.action is MiddlewareAction.PATCH and not self.patches:
            raise ValueError("PATCH decision 必须提供 patches")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class MiddlewareEvaluation:
    ability_id: str
    ability_version: str
    priority: int
    lifecycle_point: ExtensionLifecyclePoint
    action: MiddlewareAction
    reason: str = ""
    patch_count: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ability_id": self.ability_id,
            "ability_version": self.ability_version,
            "priority": self.priority,
            "lifecycle_point": self.lifecycle_point.value,
            "action": self.action.value,
            "reason": self.reason,
            "patch_count": self.patch_count,
            "error": self.error,
        }


@dataclass(frozen=True)
class MiddlewarePipelineResult:
    lifecycle_point: ExtensionLifecyclePoint
    action: MiddlewareAction
    patches: tuple[StatePatch, ...] = ()
    reason: str = ""
    short_circuit_text: str = ""
    evaluations: tuple[MiddlewareEvaluation, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "lifecycle_point",
            ExtensionLifecyclePoint(self.lifecycle_point))
        object.__setattr__(self, "action", MiddlewareAction(self.action))
        object.__setattr__(self, "patches", tuple(self.patches))
        object.__setattr__(
            self, "evaluations", tuple(self.evaluations))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "lifecycle_point": self.lifecycle_point.value,
            "action": self.action.value,
            "patches": [item.to_dict() for item in self.patches],
            "reason": self.reason,
            "short_circuit_text": self.short_circuit_text,
            "evaluations": [item.to_dict() for item in self.evaluations],
            "metadata": dict(self.metadata),
        }


class LoopMiddlewarePort(Protocol):
    @property
    def manifests(self) -> tuple[AbilityManifest, ...]: ...

    async def evaluate(
        self,
        point: ExtensionLifecyclePoint | str,
        context: MiddlewareContext,
    ) -> MiddlewarePipelineResult: ...


class LoopAbilityPort(Protocol):
    @property
    def manifest(self) -> AbilityManifest: ...

    async def evaluate(
        self,
        context: MiddlewareContext,
    ) -> MiddlewareDecision: ...


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


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


__all__ = [
    "AbilityManifest",
    "ExtensionLifecyclePoint",
    "LoopAbilityPort",
    "LoopMiddlewarePort",
    "MiddlewareAction",
    "MiddlewareContext",
    "MiddlewareDecision",
    "MiddlewareEvaluation",
    "MiddlewareFailurePolicy",
    "MiddlewarePipelineResult",
    "PatchOperation",
    "PatchScope",
    "StatePatch",
]
