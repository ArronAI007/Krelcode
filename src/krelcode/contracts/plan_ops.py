"""Pure parsing and adaptation helpers for structured plan controls."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from typing import Mapping, Sequence

from .plan import (
    PlanApprovalStatus,
    PlanControl,
    PlanControlMode,
    PlanNode,
    PlanNodeStatus,
)


def parse_plan_nodes(
    raw_items: Sequence[object],
    *,
    previous: PlanControl | None = None,
) -> tuple[PlanNode, ...]:
    """Parse tool/user input while preserving stable ids where possible."""
    if not raw_items:
        raise ValueError("计划 items 必须是非空数组")
    previous_by_objective = {
        node.objective: node.id for node in (previous.nodes if previous else ())
    }
    nodes: list[PlanNode] = []
    for index, raw in enumerate(raw_items, 1):
        if isinstance(raw, str):
            objective = raw.strip()
            status = "pending"
            record: Mapping[str, object] = {}
        elif isinstance(raw, MappingABC):
            record = raw
            objective = str(
                record.get("objective") or record.get("step") or "").strip()
            status = str(record.get("status") or "pending")
        else:
            raise ValueError(f"计划第 {index} 项必须是 object 或字符串")
        if not objective:
            raise ValueError(f"计划第 {index} 项缺少 objective/step")
        raw_id = str(record.get("id") or "").strip()
        node_id = (
            raw_id
            or previous_by_objective.get(objective, "")
            or _generated_node_id(index, objective)
        )
        nodes.append(PlanNode(
            id=node_id,
            objective=objective,
            status=status,
            depends_on=_string_tuple(record.get("depends_on")),
            parallel_group=str(record.get("parallel_group") or ""),
            owner=str(record.get("owner") or ""),
            acceptance_checks=_string_tuple(record.get("acceptance_checks")),
            evidence_refs=_string_tuple(record.get("evidence_refs")),
            metadata=_mapping(record.get("metadata")),
        ))
    return tuple(nodes)


def update_plan_control(
    current: PlanControl | None,
    nodes: Sequence[PlanNode],
    *,
    mode: PlanControlMode | str,
    trigger_observation_id: str = "",
    trigger_fact: str = "",
) -> PlanControl:
    resolved_mode = PlanControlMode(mode)
    approval = (
        current.approval
        if current is not None
        else (
            PlanApprovalStatus.PENDING
            if resolved_mode is PlanControlMode.PLAN_MODE
            else PlanApprovalStatus.NONE
        )
    )
    if current is None:
        return PlanControl.from_nodes(
            nodes,
            mode=resolved_mode,
            approval=approval,
            trigger_observation_id=trigger_observation_id,
            trigger_fact=trigger_fact or "initial plan",
        )
    return current.revise(
        nodes,
        mode=resolved_mode,
        approval=approval,
        trigger_observation_id=trigger_observation_id,
        trigger_fact=trigger_fact,
    )


def legacy_plan_control(
    items: Sequence[str],
    statuses: Sequence[str],
    *,
    active_index: int = -1,
    explanation: str = "",
    mode: PlanControlMode | str = PlanControlMode.TODO,
    approval: PlanApprovalStatus | str = PlanApprovalStatus.NONE,
) -> PlanControl:
    nodes = tuple(PlanNode(
        id=_generated_node_id(index, item),
        objective=item,
        status=(
            statuses[index - 1]
            if index - 1 < len(statuses)
            else "in_progress" if index - 1 == active_index else "pending"
        ),
    ) for index, item in enumerate(items, 1))
    return PlanControl.from_nodes(
        nodes,
        mode=mode,
        approval=approval,
        trigger_fact=explanation or "legacy plan update",
    )


def plan_snapshot_views(
    control: PlanControl,
) -> tuple[tuple[str, ...], tuple[str, ...], int, str]:
    items = tuple(node.objective for node in control.nodes)
    statuses = tuple(PlanNodeStatus(node.status).value for node in control.nodes)
    active = next(
        (
            index for index, node in enumerate(control.nodes)
            if node.status is PlanNodeStatus.IN_PROGRESS
        ),
        -1,
    )
    return items, statuses, active, control.revision.trigger_fact


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


def _generated_node_id(index: int, objective: str) -> str:
    digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()
    return f"node-{index}-{digest[:8]}"


__all__ = [
    "legacy_plan_control",
    "parse_plan_nodes",
    "plan_snapshot_views",
    "update_plan_control",
]
