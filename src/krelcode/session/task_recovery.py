"""Recovery preflight for durable tasks resumed into a live Harness."""

from __future__ import annotations

from typing import Mapping, Optional, Protocol

from ..agent.context.context_checkpoint_workspace import workspace_fingerprint
from ..contracts.application import TaskBudget
from ..contracts.runtime import RuntimeTask, RuntimeTaskState
from ..contracts.tool import ToolContext
from ..contracts.task_lifecycle import (
    TaskContinuation,
    TaskRecoveryCheck,
    TaskRecoveryDecision,
    evaluate_side_effect_replay,
)


class _RecoveryLoopPort(Protocol):
    tool_context: ToolContext


class SessionTaskRecoveryVerifier:
    """Reject stale, unauthorized or exhausted task continuations."""

    def __init__(self, loop: _RecoveryLoopPort) -> None:
        self.loop = loop

    async def verify(
        self,
        task: object,
        state: object,
        continuation: Optional[TaskContinuation],
    ) -> TaskRecoveryDecision:
        if not isinstance(task, RuntimeTask) or not isinstance(
            state, RuntimeTaskState
        ):
            return TaskRecoveryDecision(
                allowed=False,
                checks=(TaskRecoveryCheck(
                    code="contract",
                    passed=False,
                    reason="recovery verifier received invalid task state",
                ),),
            )
        checks = [
            TaskRecoveryCheck(
                code="objective",
                passed=bool(task.application.goal.strip()),
                reason=(
                    "" if task.application.goal.strip()
                    else "task objective is empty"
                ),
            ),
            TaskRecoveryCheck(
                code="continuation",
                passed=continuation is not None,
                reason=(
                    "" if continuation is not None
                    else "continuation envelope is missing"
                ),
            ),
        ]
        if continuation is None:
            return TaskRecoveryDecision(
                allowed=False,
                checks=tuple(checks),
                requires_user=True,
            )
        checks.append(TaskRecoveryCheck(
            code="objective.match",
            passed=(
                not continuation.objective
                or continuation.objective == task.application.goal
            ),
            reason=(
                "" if (
                    not continuation.objective
                    or continuation.objective == task.application.goal
                ) else "continuation objective no longer matches task"
            ),
        ))
        current_permission = ""
        tool_context = self.loop.tool_context
        current_permission = tool_context.runtime.permission_mode.value
        checks.append(TaskRecoveryCheck(
            code="permission",
            passed=(
                not continuation.permission_mode
                or not current_permission
                or continuation.permission_mode == current_permission
            ),
            reason=(
                "" if (
                    not continuation.permission_mode
                    or not current_permission
                    or continuation.permission_mode == current_permission
                ) else (
                    "permission mode changed: "
                    f"{continuation.permission_mode} -> {current_permission}"
                )
            ),
        ))
        workspace_root = tool_context.workspace_root
        if workspace_root is not None:
            current_workspace = workspace_fingerprint(
                workspace_root).as_dict()
            checks.append(TaskRecoveryCheck(
                code="workspace",
                passed=_workspace_compatible(
                    continuation.workspace_version, current_workspace),
                reason=(
                    "" if _workspace_compatible(
                        continuation.workspace_version, current_workspace)
                    else "workspace changed since continuation was captured"
                ),
                evidence={
                    "before": dict(continuation.workspace_version),
                    "current": current_workspace,
                },
            ))
        checks.append(TaskRecoveryCheck(
            code="budget",
            passed=_budget_remaining(continuation.remaining_budget),
            reason=(
                "" if _budget_remaining(continuation.remaining_budget)
                else "remaining task budget is exhausted"
            ),
        ))
        replay = evaluate_side_effect_replay(state.side_effects)
        checks.append(TaskRecoveryCheck(
            code="side_effects",
            passed=replay.allowed,
            reason=(
                replay.reason
                if not replay.allowed
                else (
                    f"{len(state.side_effects)} side effect attempt(s) retained"
                    if state.side_effects else ""
                )
            ),
            evidence={
                "effect_ids": [
                    item.effect_id for item in state.side_effects
                ],
                "replay": replay.to_dict(),
            },
        ))
        failed = [item for item in checks if not item.passed]
        requires_user = any(
            item.code in {"permission", "workspace", "side_effects"}
            for item in failed)
        return TaskRecoveryDecision(
            allowed=not failed,
            checks=tuple(checks),
            requires_user=requires_user,
            continuation=continuation,
            metadata={
                "waiting_items": list(continuation.waiting_items),
                "failed_attempts": list(continuation.failed_attempts),
            },
        )


def _workspace_compatible(
    before: object,
    current: object,
) -> bool:
    if not isinstance(before, Mapping) or not before:
        return True
    if not isinstance(current, Mapping):
        return False
    for key in ("head", "status_digest", "tree_digest"):
        expected = str(before.get(key) or "")
        actual = str(current.get(key) or "")
        if expected and actual and expected != actual:
            return False
    return True


def _budget_remaining(budget: TaskBudget) -> bool:
    limited = (
        budget.max_steps,
        budget.max_tokens,
        budget.timeout_seconds,
        budget.max_cost_usd,
        budget.max_tool_calls,
        budget.max_high_risk_actions,
    )
    if all(value is None for value in limited):
        return True
    return any(float(value) > 0 for value in limited if value is not None)


__all__ = ["SessionTaskRecoveryVerifier"]
