"""Unified mutable budget ledger for one Agent Loop turn."""

from __future__ import annotations

import time
from typing import Mapping, Optional

from krelcode.agent.pricing import estimate_cost
from krelcode.contracts.application import TaskBudget
from krelcode.contracts.budget import (
    BudgetAction,
    BudgetDimension,
    BudgetDimensionState,
    BudgetLevel,
    BudgetSnapshot,
)


class BudgetLedger:
    def __init__(
        self,
        budget: TaskBudget,
        *,
        model: str = "",
        started_at: Optional[float] = None,
    ) -> None:
        self.budget = budget
        self.model = model
        self.started_at = (
            float(started_at) if started_at is not None else time.monotonic())
        self.usage: dict[BudgetDimension, float] = {
            dimension: 0.0 for dimension in BudgetDimension
        }
        self.limits: dict[BudgetDimension, Optional[float]] = {
            BudgetDimension.STEPS: _limit(budget.max_steps),
            BudgetDimension.TOKENS: _limit(budget.max_tokens),
            BudgetDimension.COST_USD: _limit(budget.max_cost_usd),
            BudgetDimension.TOOL_CALLS: _limit(budget.max_tool_calls),
            BudgetDimension.SUBAGENTS: _limit(budget.max_subagents),
            BudgetDimension.HIGH_RISK_ACTIONS: _limit(
                budget.max_high_risk_actions),
            BudgetDimension.ELAPSED_SECONDS: _limit(
                budget.timeout_seconds),
        }
        self.soft_notified = False
        self.soft_prompted = False
        self.convergence_mode = False
        self.delegation_blocked = False
        self.extensions_used = 0

    def record_step(self) -> None:
        self.usage[BudgetDimension.STEPS] += 1

    def set_tokens(self, total: int) -> None:
        self.usage[BudgetDimension.TOKENS] = max(0, int(total))

    def record_llm_usage(
        self,
        usage: Mapping[str, int],
    ) -> Optional[float]:
        cost = estimate_cost(self.model, dict(usage))
        if cost is not None:
            self.usage[BudgetDimension.COST_USD] += cost
        return cost

    def record_tool_call(self) -> int:
        self.usage[BudgetDimension.TOOL_CALLS] += 1
        return int(self.usage[BudgetDimension.TOOL_CALLS])

    def record_subagent(self) -> int:
        self.usage[BudgetDimension.SUBAGENTS] += 1
        return int(self.usage[BudgetDimension.SUBAGENTS])

    def record_high_risk_action(self) -> int:
        self.usage[BudgetDimension.HIGH_RISK_ACTIONS] += 1
        return int(self.usage[BudgetDimension.HIGH_RISK_ACTIONS])

    def record_external_usage(self, usage: Mapping[str, float]) -> None:
        """Roll delegated child usage into the parent task ledger."""
        for raw_dimension, raw_value in usage.items():
            try:
                dimension = BudgetDimension(str(raw_dimension))
            except ValueError:
                continue
            self.usage[dimension] += max(0.0, float(raw_value))

    def usage_record(self) -> dict[str, float]:
        return {
            dimension.value: float(value)
            for dimension, value in self.usage.items()
        }

    def remaining_task_budget(self) -> TaskBudget:
        """Return the residual hard limits available to delegated work."""
        def remaining(name: str) -> Optional[int]:
            limit = self.limits.get(BudgetDimension(name))
            if limit is None:
                return None
            return max(0, int(limit - self.usage[BudgetDimension(name)]))

        timeout = self.limits.get(BudgetDimension.ELAPSED_SECONDS)
        cost_limit = self.limits.get(BudgetDimension.COST_USD)
        remaining_timeout = (
            None if timeout is None else
            max(0.0, timeout - self.usage[BudgetDimension.ELAPSED_SECONDS])
        )
        return TaskBudget(
            max_steps=remaining(BudgetDimension.STEPS.value),
            max_tokens=remaining(BudgetDimension.TOKENS.value),
            timeout_seconds=remaining_timeout,
            max_cost_usd=(
                None if cost_limit is None
                else max(
                    0.0, cost_limit - self.usage[BudgetDimension.COST_USD])
            ),
            max_tool_calls=remaining(BudgetDimension.TOOL_CALLS.value),
            max_subagents=remaining(BudgetDimension.SUBAGENTS.value),
            max_high_risk_actions=remaining(
                BudgetDimension.HIGH_RISK_ACTIONS.value),
            soft_limit_ratio=self.budget.soft_limit_ratio,
            ask_user_at_soft_limit=self.budget.ask_user_at_soft_limit,
            allow_budget_extension=self.budget.allow_budget_extension,
            budget_extension_ratio=self.budget.budget_extension_ratio,
        )

    def snapshot(self) -> BudgetSnapshot:
        elapsed = max(0.0, time.monotonic() - self.started_at)
        self.usage[BudgetDimension.ELAPSED_SECONDS] = elapsed
        dimensions = tuple(
            self._dimension_state(dimension)
            for dimension in BudgetDimension
            if self.limits.get(dimension) is not None
        )
        soft = tuple(
            BudgetDimension(item.dimension).value for item in dimensions
            if item.level is BudgetLevel.SOFT_LIMIT
        )
        hard = tuple(
            BudgetDimension(item.dimension).value for item in dimensions
            if item.level is BudgetLevel.HARD_LIMIT
        )
        if hard:
            action = (
                BudgetAction.ASK_USER
                if self.budget.allow_budget_extension
                else BudgetAction.STOP
            )
        elif soft and not self.soft_notified:
            action = (
                BudgetAction.ASK_USER
                if self.budget.ask_user_at_soft_limit
                else BudgetAction.CONVERGE
            )
        elif self.convergence_mode:
            action = BudgetAction.CONVERGE
        else:
            action = BudgetAction.CONTINUE
        return BudgetSnapshot(
            action=action,
            dimensions=dimensions,
            soft_dimensions=soft,
            hard_dimensions=hard,
            convergence_mode=self.convergence_mode,
            delegation_blocked=self.delegation_blocked,
            extensions_used=self.extensions_used,
        )

    def enable_convergence(self, *, block_delegation: bool = True) -> None:
        self.soft_notified = True
        self.convergence_mode = True
        self.delegation_blocked = block_delegation

    def mark_soft_notified(self) -> None:
        self.soft_notified = True

    def extend_limits(self, ratio: Optional[float] = None) -> None:
        growth = (
            ratio
            if ratio is not None
            else self.budget.budget_extension_ratio
        )
        growth = max(0.1, float(growth))
        for dimension, limit in tuple(self.limits.items()):
            if limit is None:
                continue
            self.limits[dimension] = max(
                limit,
                limit * (1.0 + growth),
            )
        self.extensions_used += 1
        self.soft_notified = False
        self.soft_prompted = False
        self.convergence_mode = False
        self.delegation_blocked = False

    def can_start_tool_call(self) -> tuple[bool, str]:
        hard = self._hard_limit(BudgetDimension.TOOL_CALLS)
        if hard is not None and self.usage[BudgetDimension.TOOL_CALLS] >= hard:
            return False, "tool call 预算已耗尽"
        return True, ""

    def can_start_subagent(self) -> tuple[bool, str]:
        if self.delegation_blocked:
            return False, "软预算状态下已冻结新 Subagent"
        hard = self._hard_limit(BudgetDimension.SUBAGENTS)
        if hard is not None and self.usage[BudgetDimension.SUBAGENTS] >= hard:
            return False, "Subagent 预算已耗尽"
        return True, ""

    def can_start_high_risk_action(self) -> tuple[bool, str]:
        hard = self._hard_limit(BudgetDimension.HIGH_RISK_ACTIONS)
        if (
            hard is not None
            and self.usage[BudgetDimension.HIGH_RISK_ACTIONS] >= hard
        ):
            return False, "高风险动作预算已耗尽"
        return True, ""

    def _dimension_state(
        self,
        dimension: BudgetDimension,
    ) -> BudgetDimensionState:
        used = float(self.usage.get(dimension, 0.0))
        hard = self.limits.get(dimension)
        soft = (
            hard * self.budget.soft_limit_ratio
            if hard is not None else None
        )
        remaining: Optional[float]
        if hard is not None and used > hard:
            level = BudgetLevel.HARD_LIMIT
            remaining = 0.0
        elif soft is not None and used >= soft:
            level = BudgetLevel.SOFT_LIMIT
            remaining = max(0.0, hard - used) if hard is not None else None
        else:
            level = BudgetLevel.NORMAL
            remaining = max(0.0, hard - used) if hard is not None else None
        return BudgetDimensionState(
            dimension=dimension,
            used=used,
            soft_limit=soft,
            hard_limit=hard,
            level=level,
            remaining=remaining,
        )

    def _hard_limit(self, dimension: BudgetDimension) -> Optional[float]:
        return self.limits.get(dimension)


def _limit(value: Optional[int | float]) -> Optional[float]:
    return float(value) if value is not None else None


__all__ = ["BudgetLedger"]
