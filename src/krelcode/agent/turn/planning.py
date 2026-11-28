"""Plan control policy for selecting a planning surface."""

from __future__ import annotations

from typing import TypeAlias

from krelcode.contracts.application import (
    ApplicationRequest,
    RiskLevel,
    TaskShape,
)
from krelcode.contracts.plan import (
    PlanControl,
    PlanControlMode,
    PlanModeControl,
    PlannerExecutorControl,
    TodoControl,
)
from krelcode.contracts.plan_ops import (
    legacy_plan_control,
    parse_plan_nodes,
    plan_snapshot_views,
    update_plan_control,
)
from krelcode.contracts.runtime import PermissionMode

PlanControlAdapter: TypeAlias = (
    TodoControl | PlanModeControl | PlannerExecutorControl
)


class PlanControlPolicy:
    """Select and wrap a planning control surface from task complexity."""

    def select_mode(
        self,
        request: ApplicationRequest | None,
        *,
        permission_mode: PermissionMode | str = PermissionMode.DEFAULT,
        explicit_mode: PlanControlMode | str | None = None,
    ) -> PlanControlMode:
        if explicit_mode is not None:
            return PlanControlMode(explicit_mode)
        if PermissionMode(permission_mode) is PermissionMode.PLAN:
            return PlanControlMode.PLAN_MODE
        if request is None:
            return PlanControlMode.TODO
        if request.task_shape in {
            TaskShape.LONG_HORIZON,
            TaskShape.MULTI_AGENT,
            TaskShape.HYBRID,
            TaskShape.WORKFLOW,
        }:
            return PlanControlMode.PLANNER_EXECUTOR
        if request.risk.level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PlanControlMode.PLAN_MODE
        budget = request.budget
        if (
            budget.max_steps is not None
            and budget.max_steps > 16
        ) or (
            budget.max_subagents is not None
            and budget.max_subagents > 1
        ):
            return PlanControlMode.PLANNER_EXECUTOR
        return PlanControlMode.TODO

    def control_type(
        self,
        mode: PlanControlMode | str,
    ) -> (
        type[TodoControl]
        | type[PlanModeControl]
        | type[PlannerExecutorControl]
    ):
        resolved = PlanControlMode(mode)
        if resolved is PlanControlMode.PLAN_MODE:
            return PlanModeControl
        if resolved is PlanControlMode.PLANNER_EXECUTOR:
            return PlannerExecutorControl
        return TodoControl

    def control(
        self,
        mode: PlanControlMode | str,
    ) -> (
        type[TodoControl]
        | type[PlanModeControl]
        | type[PlannerExecutorControl]
    ):
        return self.control_type(mode)

    def wrap(self, control: PlanControl) -> PlanControlAdapter:
        mode = PlanControlMode(control.mode)
        if mode is PlanControlMode.PLAN_MODE:
            return PlanModeControl(control)
        if mode is PlanControlMode.PLANNER_EXECUTOR:
            return PlannerExecutorControl(control)
        return TodoControl(control)


__all__ = [
    "PlanControlAdapter",
    "PlanControlPolicy",
    "legacy_plan_control",
    "parse_plan_nodes",
    "plan_snapshot_views",
    "update_plan_control",
]
