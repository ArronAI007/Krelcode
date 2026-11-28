"""Structured task planning and execution-control contracts."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Sequence


class PlanControlMode(str, Enum):
    TODO = "todo"
    PLAN_MODE = "plan_mode"
    PLANNER_EXECUTOR = "planner_executor"


class PlanApprovalStatus(str, Enum):
    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class PlanNodeStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanRole(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"
    OWNER = "owner"


@dataclass(frozen=True)
class PlanNode:
    id: str
    objective: str
    status: PlanNodeStatus | str = PlanNodeStatus.PENDING
    depends_on: tuple[str, ...] = ()
    parallel_group: str = ""
    owner: str = ""
    acceptance_checks: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("id", "objective", "parallel_group", "owner"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.id:
            raise ValueError("PlanNode.id 不能为空")
        if not self.objective:
            raise ValueError(f"PlanNode.objective 不能为空: {self.id}")
        object.__setattr__(self, "status", PlanNodeStatus(self.status))
        for name in ("depends_on", "acceptance_checks", "evidence_refs"):
            values = getattr(self, name)
            if isinstance(values, str):
                values = (values,)
            object.__setattr__(
                self,
                name,
                tuple(dict.fromkeys(
                    str(item).strip()
                    for item in values
                    if str(item).strip()
                )),
            )
        if self.id in self.depends_on:
            raise ValueError(f"PlanNode 不能依赖自己: {self.id}")
        if (
            PlanNodeStatus(self.status) is PlanNodeStatus.COMPLETED
            and self.acceptance_checks
            and not self.evidence_refs
        ):
            raise ValueError(
                f"PlanNode {self.id} 已完成但缺少 evidence_refs")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def role(self) -> PlanRole:
        """Resolve the node owner's control role.

        Owners use either a role name (``verifier``), a role-qualified
        principal (``executor:backend``), or metadata ``{"role": "planner"}``.
        Unknown principals remain regular executors.
        """
        raw = self.metadata.get("role")
        if raw is None and self.owner:
            raw = self.owner.partition(":")[0]
        try:
            return PlanRole(str(raw or PlanRole.EXECUTOR.value))
        except ValueError:
            return PlanRole.EXECUTOR

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "objective": self.objective,
            "status": PlanNodeStatus(self.status).value,
            "depends_on": list(self.depends_on),
            "parallel_group": self.parallel_group,
            "owner": self.owner,
            "acceptance_checks": list(self.acceptance_checks),
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PlanNode":
        return cls(
            id=str(raw.get("id") or ""),
            objective=str(raw.get("objective") or raw.get("step") or ""),
            status=str(raw.get("status") or "pending"),
            depends_on=_string_tuple(raw.get("depends_on")),
            parallel_group=str(raw.get("parallel_group") or ""),
            owner=str(raw.get("owner") or ""),
            acceptance_checks=_string_tuple(raw.get("acceptance_checks")),
            evidence_refs=_string_tuple(raw.get("evidence_refs")),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class PlanRevision:
    revision_id: str
    parent_revision_id: str = ""
    trigger_observation_id: str = ""
    trigger_fact: str = ""
    preserved_completed_ids: tuple[str, ...] = ()
    added_ids: tuple[str, ...] = ()
    removed_ids: tuple[str, ...] = ()
    modified_ids: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "revision_id", "parent_revision_id", "trigger_observation_id",
            "trigger_fact",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.revision_id:
            raise ValueError("PlanRevision.revision_id 不能为空")
        for name in (
            "preserved_completed_ids", "added_ids", "removed_ids",
            "modified_ids",
        ):
            object.__setattr__(
                self, name, _string_tuple(getattr(self, name)))
        object.__setattr__(self, "created_at", max(0.0, float(self.created_at)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_dict(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "parent_revision_id": self.parent_revision_id,
            "trigger_observation_id": self.trigger_observation_id,
            "trigger_fact": self.trigger_fact,
            "preserved_completed_ids": list(self.preserved_completed_ids),
            "added_ids": list(self.added_ids),
            "removed_ids": list(self.removed_ids),
            "modified_ids": list(self.modified_ids),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PlanRevision":
        return cls(
            revision_id=str(raw.get("revision_id") or ""),
            parent_revision_id=str(raw.get("parent_revision_id") or ""),
            trigger_observation_id=str(
                raw.get("trigger_observation_id") or ""),
            trigger_fact=str(raw.get("trigger_fact") or ""),
            preserved_completed_ids=_string_tuple(
                raw.get("preserved_completed_ids")),
            added_ids=_string_tuple(raw.get("added_ids")),
            removed_ids=_string_tuple(raw.get("removed_ids")),
            modified_ids=_string_tuple(raw.get("modified_ids")),
            created_at=_coerce_float(raw.get("created_at")),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class PlanControl:
    mode: PlanControlMode | str
    revision: PlanRevision
    nodes: tuple[PlanNode, ...] = ()
    approval: PlanApprovalStatus | str = PlanApprovalStatus.NONE
    revision_history: tuple[PlanRevision, ...] = ()
    updated_at: float = 0.0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", PlanControlMode(self.mode))
        object.__setattr__(self, "approval", PlanApprovalStatus(self.approval))
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(
            self, "revision_history", tuple(self.revision_history))
        _validate_nodes(self.nodes, PlanControlMode(self.mode))
        object.__setattr__(self, "updated_at", max(0.0, float(self.updated_at)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def active_nodes(self) -> tuple[PlanNode, ...]:
        return tuple(
            node for node in self.nodes
            if node.status is PlanNodeStatus.IN_PROGRESS)

    @property
    def completed_ids(self) -> tuple[str, ...]:
        return tuple(
            node.id for node in self.nodes
            if node.status is PlanNodeStatus.COMPLETED)

    def nodes_for_role(self, role: PlanRole | str) -> tuple[PlanNode, ...]:
        resolved = PlanRole(role)
        return tuple(node for node in self.nodes if node.role is resolved)

    @property
    def planner_nodes(self) -> tuple[PlanNode, ...]:
        return self.nodes_for_role(PlanRole.PLANNER)

    @property
    def executor_nodes(self) -> tuple[PlanNode, ...]:
        return self.nodes_for_role(PlanRole.EXECUTOR)

    @property
    def verifier_nodes(self) -> tuple[PlanNode, ...]:
        return self.nodes_for_role(PlanRole.VERIFIER)

    def with_approval(
        self,
        approval: PlanApprovalStatus | str,
    ) -> "PlanControl":
        return PlanControl(
            mode=self.mode,
            revision=self.revision,
            nodes=self.nodes,
            approval=PlanApprovalStatus(approval),
            revision_history=self.revision_history,
            updated_at=self.updated_at,
            metadata=self.metadata,
        )

    def revise(
        self,
        nodes: Sequence[PlanNode],
        *,
        trigger_observation_id: str = "",
        trigger_fact: str = "",
        mode: Optional[PlanControlMode | str] = None,
        approval: Optional[PlanApprovalStatus | str] = None,
    ) -> "PlanControl":
        new_nodes = tuple(nodes)
        previous = {
            node.id: node for node in self.nodes
        }
        current = {
            node.id: node for node in new_nodes
        }
        previous_completed = set(self.completed_ids)
        missing_completed = previous_completed - {
            node_id for node_id, node in current.items()
            if node.status is PlanNodeStatus.COMPLETED
        }
        if missing_completed:
            raise ValueError(
                "重规划不能删除或回退已完成节点: "
                + ", ".join(sorted(missing_completed)))
        for node_id in previous_completed & set(current):
            old = previous[node_id]
            new = current[node_id]
            immutable = (
                "objective", "depends_on", "parallel_group", "owner",
                "acceptance_checks",
            )
            changed = tuple(
                name for name in immutable
                if getattr(new, name) != getattr(old, name)
            )
            if changed:
                raise ValueError(
                    "重规划不能改写已完成节点 "
                    f"{node_id} 的字段: {', '.join(changed)}")
            if not set(old.evidence_refs).issubset(new.evidence_refs):
                raise ValueError(
                    f"重规划不能删除已完成节点 {node_id} 的 evidence_refs")
        added = tuple(sorted(set(current) - set(previous)))
        removed = tuple(sorted(set(previous) - set(current)))
        modified = tuple(sorted(
            node_id for node_id in set(previous) & set(current)
            if previous[node_id] != current[node_id]
        ))
        resolved_mode = PlanControlMode(mode or self.mode)
        if (
            self.nodes
            and (
                added or removed or modified
                or resolved_mode is not PlanControlMode(self.mode)
            )
            and not trigger_fact.strip()
        ):
            raise ValueError("计划修订必须提供 trigger_fact")
        payload = {
            "mode": resolved_mode.value,
            "parent": self.revision.revision_id,
            "trigger_observation_id": trigger_observation_id,
            "trigger_fact": trigger_fact,
            "nodes": [node.as_dict() for node in new_nodes],
        }
        revision_hash = hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
        revision = PlanRevision(
            revision_id=f"plan-{revision_hash[:16]}",
            parent_revision_id=self.revision.revision_id,
            trigger_observation_id=trigger_observation_id,
            trigger_fact=trigger_fact,
            preserved_completed_ids=tuple(sorted(
                previous_completed & set(current))),
            added_ids=added,
            removed_ids=removed,
            modified_ids=modified,
            created_at=time.time(),
        )
        history = (
            *self.revision_history,
            self.revision,
        )
        return PlanControl(
            mode=resolved_mode,
            revision=revision,
            nodes=new_nodes,
            approval=(
                self.approval if approval is None
                else PlanApprovalStatus(approval)
            ),
            revision_history=history,
            updated_at=revision.created_at,
            metadata=self.metadata,
        )

    @classmethod
    def empty(
        cls,
        mode: PlanControlMode | str = PlanControlMode.TODO,
    ) -> "PlanControl":
        return cls(
            mode=mode,
            revision=PlanRevision(
                revision_id="plan-root",
                trigger_fact="plan initialized",
                created_at=time.time(),
            ),
            nodes=(),
            approval=PlanApprovalStatus.NONE,
            updated_at=time.time(),
        )

    @classmethod
    def from_nodes(
        cls,
        nodes: Sequence[PlanNode],
        *,
        mode: PlanControlMode | str = PlanControlMode.TODO,
        approval: PlanApprovalStatus | str = PlanApprovalStatus.NONE,
        trigger_observation_id: str = "",
        trigger_fact: str = "",
    ) -> "PlanControl":
        control = cls.empty(mode)
        return control.revise(
            nodes,
            mode=mode,
            approval=approval,
            trigger_observation_id=trigger_observation_id,
            trigger_fact=trigger_fact,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": PlanControlMode(self.mode).value,
            "revision": self.revision.as_dict(),
            "nodes": [node.as_dict() for node in self.nodes],
            "approval": PlanApprovalStatus(self.approval).value,
            "revision_history": [
                item.as_dict() for item in self.revision_history
            ],
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PlanControl":
        revision_raw = raw.get("revision")
        revision = (
            PlanRevision.from_mapping(revision_raw)
            if isinstance(revision_raw, Mapping)
            else PlanRevision(revision_id="plan-root")
        )
        nodes_raw = raw.get("nodes")
        nodes = tuple(
            PlanNode.from_mapping(item)
            for item in (nodes_raw if isinstance(nodes_raw, list) else ())
            if isinstance(item, Mapping)
        )
        history_raw = raw.get("revision_history")
        history = tuple(
            PlanRevision.from_mapping(item)
            for item in (
                history_raw if isinstance(history_raw, list) else ()
            )
            if isinstance(item, Mapping)
        )
        return cls(
            mode=str(raw.get("mode") or "todo"),
            revision=revision,
            nodes=nodes,
            approval=str(raw.get("approval") or "none"),
            revision_history=history,
            updated_at=_coerce_float(raw.get("updated_at")),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class TodoControl:
    """Lightweight ordered control for short, feedback-heavy tasks."""

    control: PlanControl
    mode = PlanControlMode.TODO
    requires_approval = False
    supports_parallel = False

    def __post_init__(self) -> None:
        if PlanControlMode(self.control.mode) is not self.mode:
            raise ValueError("TodoControl 只接受 todo 计划")

    def ready_nodes(self) -> tuple[PlanNode, ...]:
        return PlanScheduler().runnable_nodes(self.control)


@dataclass(frozen=True)
class PlanModeControl:
    """Read-only exploration followed by explicit human approval."""

    control: PlanControl
    mode = PlanControlMode.PLAN_MODE
    requires_approval = True
    supports_parallel = False

    def __post_init__(self) -> None:
        if PlanControlMode(self.control.mode) is not self.mode:
            raise ValueError("PlanModeControl 只接受 plan_mode 计划")

    def ready_nodes(self) -> tuple[PlanNode, ...]:
        if self.control.approval is not PlanApprovalStatus.APPROVED:
            return ()
        return PlanScheduler().runnable_nodes(self.control)[:1]


@dataclass(frozen=True)
class PlannerExecutorControl:
    """Role-aware planner/executor/verifier control for complex tasks."""

    control: PlanControl
    mode = PlanControlMode.PLANNER_EXECUTOR
    requires_approval = False
    supports_parallel = True

    def __post_init__(self) -> None:
        if PlanControlMode(self.control.mode) is not self.mode:
            raise ValueError(
                "PlannerExecutorControl 只接受 planner_executor 计划")
        roles = {node.role for node in self.control.nodes}
        required = {
            PlanRole.PLANNER,
            PlanRole.EXECUTOR,
            PlanRole.VERIFIER,
        }
        if self.control.nodes and not required.issubset(roles):
            missing = ", ".join(sorted(role.value for role in required - roles))
            raise ValueError(
                "planner_executor 计划缺少角色 owner: " + missing)

    def ready_nodes(self) -> tuple[PlanNode, ...]:
        if self.control.approval is PlanApprovalStatus.PENDING:
            return ()
        return PlanScheduler().runnable_nodes(self.control)


@dataclass(frozen=True)
class PlanScheduler:
    """Return nodes that can run under dependency and parallel constraints."""

    def ready_nodes(self, control: PlanControl) -> tuple[PlanNode, ...]:
        completed = set(control.completed_ids)
        ready = [
            node for node in control.nodes
            if node.status is PlanNodeStatus.PENDING
            and all(dep in completed for dep in node.depends_on)
        ]
        return tuple(sorted(
            ready,
            key=lambda node: (
                node.parallel_group,
                node.id,
            ),
        ))

    def runnable_nodes(self, control: PlanControl) -> tuple[PlanNode, ...]:
        if control.approval is PlanApprovalStatus.PENDING:
            return ()
        ready = self.ready_nodes(control)
        if PlanControlMode(control.mode) is PlanControlMode.PLANNER_EXECUTOR:
            return ready
        return ready[:1]

    def parallel_groups(self, control: PlanControl) -> Mapping[str, tuple[str, ...]]:
        groups: dict[str, list[str]] = {}
        for node in control.nodes:
            if node.parallel_group:
                groups.setdefault(node.parallel_group, []).append(node.id)
        return MappingProxyType({
            key: tuple(sorted(value)) for key, value in groups.items()
        })

    def execution_wave(self, control: PlanControl) -> tuple[PlanNode, ...]:
        """Return one dependency-safe frontier, including parallel siblings."""
        return self.runnable_nodes(control)


def _validate_nodes(
    nodes: tuple[PlanNode, ...],
    mode: PlanControlMode,
) -> None:
    ids = [node.id for node in nodes]
    if len(ids) != len(set(ids)):
        raise ValueError("PlanNode.id 必须唯一")
    known = set(ids)
    for node in nodes:
        missing = set(node.depends_on) - known
        if missing:
            raise ValueError(
                f"PlanNode {node.id} 依赖未知节点: "
                + ", ".join(sorted(missing)))
        if node.status in {
            PlanNodeStatus.IN_PROGRESS,
            PlanNodeStatus.COMPLETED,
        }:
            incomplete_dependencies = tuple(
                dependency
                for dependency in node.depends_on
                if by_id_status(nodes, dependency) is not PlanNodeStatus.COMPLETED
            )
            if incomplete_dependencies:
                raise ValueError(
                    f"PlanNode {node.id} 已开始但依赖未完成: "
                    + ", ".join(incomplete_dependencies))
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {node.id: node for node in nodes}

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError(f"计划依赖存在环: {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dep in by_id[node_id].depends_on:
            visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in ids:
        visit(node_id)
    active = [
        node for node in nodes
        if node.status is PlanNodeStatus.IN_PROGRESS
    ]
    if len(active) > 1:
        groups = {node.parallel_group for node in active}
        if (
            mode is not PlanControlMode.PLANNER_EXECUTOR
            or "" in groups
            or len(groups) != 1
        ):
            raise ValueError(
                "计划最多一个 in_progress；并行执行必须属于同一 "
                "parallel_group 且使用 planner_executor 模式")
    if mode is PlanControlMode.PLANNER_EXECUTOR and any(
        not node.owner for node in nodes
    ):
        raise ValueError("planner_executor 节点必须设置 owner")
    if mode is PlanControlMode.PLANNER_EXECUTOR and nodes:
        roles = {node.role for node in nodes}
        required = {
            PlanRole.PLANNER,
            PlanRole.EXECUTOR,
            PlanRole.VERIFIER,
        }
        role_missing = required - roles
        if role_missing:
            raise ValueError(
                "planner_executor 计划缺少角色 owner: "
                + ", ".join(
                    sorted(role.value for role in role_missing)))


def by_id_status(
    nodes: Sequence[PlanNode],
    node_id: str,
) -> PlanNodeStatus:
    for node in nodes:
        if node.id == node_id:
            return PlanNodeStatus(node.status)
    raise ValueError(f"未知 PlanNode.id: {node_id}")


def _mapping(raw: object) -> dict[str, object]:
    return dict(raw) if isinstance(raw, MappingABC) else {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, SequenceABC) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        values = value
    else:
        values = ()
    return tuple(
        text for item in values
        if (text := str(item).strip())
    )


def _coerce_float(value: object) -> float:
    try:
        return max(0.0, float(str(value)))
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "PlanApprovalStatus",
    "PlanControl",
    "PlanControlMode",
    "PlanModeControl",
    "PlanNode",
    "PlanNodeStatus",
    "PlanRevision",
    "PlanRole",
    "PlanScheduler",
    "PlannerExecutorControl",
    "TodoControl",
]
