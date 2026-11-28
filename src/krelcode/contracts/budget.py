"""Unified task budget usage, thresholds and unfinished manifests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional


class BudgetDimension(str, Enum):
    STEPS = "steps"
    TOKENS = "tokens"
    COST_USD = "cost_usd"
    TOOL_CALLS = "tool_calls"
    SUBAGENTS = "subagents"
    HIGH_RISK_ACTIONS = "high_risk_actions"
    ELAPSED_SECONDS = "elapsed_seconds"


class BudgetLevel(str, Enum):
    NORMAL = "normal"
    SOFT_LIMIT = "soft_limit"
    HARD_LIMIT = "hard_limit"


class BudgetAction(str, Enum):
    CONTINUE = "continue"
    CONVERGE = "converge"
    ASK_USER = "ask_user"
    EXTEND = "extend"
    PARTIAL_DELIVERY = "partial_delivery"
    STOP = "stop"


@dataclass(frozen=True)
class BudgetLimit:
    soft: Optional[float] = None
    hard: Optional[float] = None

    def as_record(self) -> dict[str, object]:
        return {"soft": self.soft, "hard": self.hard}


@dataclass(frozen=True)
class BudgetDimensionState:
    dimension: BudgetDimension | str
    used: float
    soft_limit: Optional[float]
    hard_limit: Optional[float]
    level: BudgetLevel | str
    remaining: Optional[float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "dimension", BudgetDimension(self.dimension))
        object.__setattr__(self, "level", BudgetLevel(self.level))
        object.__setattr__(self, "used", max(0.0, float(self.used)))
        if self.soft_limit is not None:
            object.__setattr__(
                self, "soft_limit", max(0.0, float(self.soft_limit)))
        if self.hard_limit is not None:
            object.__setattr__(
                self, "hard_limit", max(0.0, float(self.hard_limit)))
        if self.remaining is not None:
            object.__setattr__(
                self, "remaining", max(0.0, float(self.remaining)))

    def as_record(self) -> dict[str, object]:
        return {
            "dimension": BudgetDimension(self.dimension).value,
            "used": self.used,
            "soft_limit": self.soft_limit,
            "hard_limit": self.hard_limit,
            "level": BudgetLevel(self.level).value,
            "remaining": self.remaining,
        }


@dataclass(frozen=True)
class BudgetSnapshot:
    action: BudgetAction | str
    dimensions: tuple[BudgetDimensionState, ...] = ()
    soft_dimensions: tuple[str, ...] = ()
    hard_dimensions: tuple[str, ...] = ()
    convergence_mode: bool = False
    delegation_blocked: bool = False
    extensions_used: int = 0
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", BudgetAction(self.action))
        object.__setattr__(self, "dimensions", tuple(self.dimensions))
        object.__setattr__(
            self,
            "soft_dimensions",
            tuple(dict.fromkeys(
                str(item) for item in self.soft_dimensions if str(item))),
        )
        object.__setattr__(
            self,
            "hard_dimensions",
            tuple(dict.fromkeys(
                str(item) for item in self.hard_dimensions if str(item))),
        )
        object.__setattr__(
            self, "extensions_used", max(0, int(self.extensions_used)))

    @property
    def exhausted(self) -> bool:
        return BudgetAction(self.action) in {
            BudgetAction.PARTIAL_DELIVERY,
            BudgetAction.STOP,
        }

    def as_record(self) -> dict[str, object]:
        return {
            "action": BudgetAction(self.action).value,
            "dimensions": [
                item.as_record() for item in self.dimensions
            ],
            "soft_dimensions": list(self.soft_dimensions),
            "hard_dimensions": list(self.hard_dimensions),
            "convergence_mode": self.convergence_mode,
            "delegation_blocked": self.delegation_blocked,
            "extensions_used": self.extensions_used,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class UnfinishedItem:
    kind: str
    id: str
    summary: str
    status: str = "pending"
    reason: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("kind", "id", "summary", "status", "reason"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.summary:
            raise ValueError("UnfinishedItem.summary 不能为空")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_record(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.id,
            "summary": self.summary,
            "status": self.status,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class UnfinishedManifest:
    goal: str
    reason: str
    items: tuple[UnfinishedItem, ...] = ()
    completed: tuple[str, ...] = ()
    partial_deliverables: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    budget: Optional[BudgetSnapshot] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("goal", "reason"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "items", tuple(self.items))
        for name in ("completed", "partial_deliverables", "next_steps"):
            object.__setattr__(
                self,
                name,
                tuple(dict.fromkeys(
                    str(item).strip()
                    for item in getattr(self, name)
                    if str(item).strip()
                )),
            )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_record(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "reason": self.reason,
            "items": [item.as_record() for item in self.items],
            "completed": list(self.completed),
            "partial_deliverables": list(self.partial_deliverables),
            "next_steps": list(self.next_steps),
            "budget": (
                self.budget.as_record() if self.budget is not None else None),
            "metadata": dict(self.metadata),
        }


__all__ = [
    "BudgetAction",
    "BudgetDimension",
    "BudgetDimensionState",
    "BudgetLevel",
    "BudgetLimit",
    "BudgetSnapshot",
    "UnfinishedItem",
    "UnfinishedManifest",
]
