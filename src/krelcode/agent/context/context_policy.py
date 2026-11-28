"""Deterministic global token policy for context layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from ...agent.context.context_contracts import (
    CATEGORY_KNOWLEDGE,
    CATEGORY_MEMORY,
    CATEGORY_RUNTIME,
    CATEGORY_SYSTEM,
    CATEGORY_WORKSPACE,
    ContextLayer,
)


@dataclass(frozen=True)
class ContextBudgetDecision:
    layer_id: str
    category: str
    tokens: int
    selected: bool
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "layer_id": self.layer_id,
            "category": self.category,
            "tokens": self.tokens,
            "selected": self.selected,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ContextBudgetPolicy:
    max_tokens: int = 16_000
    category_caps: Mapping[str, int] = field(default_factory=lambda: {
        CATEGORY_SYSTEM: 0,
        CATEGORY_WORKSPACE: 3_500,
        CATEGORY_KNOWLEDGE: 1_800,
        CATEGORY_MEMORY: 1_200,
        CATEGORY_RUNTIME: 2_000,
    })
    required_categories: tuple[str, ...] = (
        CATEGORY_SYSTEM,
        CATEGORY_WORKSPACE,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_tokens", max(256, int(self.max_tokens)))
        object.__setattr__(
            self,
            "category_caps",
            MappingProxyType({
                str(key): max(0, int(value))
                for key, value in self.category_caps.items()
            }),
        )
        object.__setattr__(
            self,
            "required_categories",
            tuple(dict.fromkeys(
                str(item) for item in self.required_categories
            )),
        )

    def apply(
        self,
        layers: Sequence[ContextLayer],
    ) -> tuple[tuple[ContextLayer, ...], tuple[ContextBudgetDecision, ...]]:
        selected: list[ContextLayer] = []
        decisions: list[ContextBudgetDecision] = []
        total = 0
        by_category: dict[str, int] = {}
        for layer in layers:
            tokens = layer.tokens
            cap = self.category_caps.get(layer.category, 0)
            category_tokens = by_category.get(layer.category, 0)
            required = layer.category in self.required_categories
            reason = ""
            if (
                not required
                and cap > 0
                and category_tokens + tokens > cap
            ):
                reason = f"category-cap:{layer.category}:{cap}"
            elif not required and total + tokens > self.max_tokens:
                reason = f"total-cap:{self.max_tokens}"
            if reason:
                decisions.append(ContextBudgetDecision(
                    layer_id=layer.id,
                    category=layer.category,
                    tokens=tokens,
                    selected=False,
                    reason=reason,
                ))
                continue
            selected.append(layer)
            total += tokens
            by_category[layer.category] = category_tokens + tokens
            decisions.append(ContextBudgetDecision(
                layer_id=layer.id,
                category=layer.category,
                tokens=tokens,
                selected=True,
            ))
        return tuple(selected), tuple(decisions)


__all__ = ["ContextBudgetDecision", "ContextBudgetPolicy"]
