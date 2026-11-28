"""Deterministic global token policy for context layers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from ...agent.context.context_contracts import (
    CATEGORY_KNOWLEDGE,
    CATEGORY_MEMORY,
    CATEGORY_RUNTIME,
    CATEGORY_SYSTEM,
    CATEGORY_WORKSPACE,
    CATEGORY_ORDER,
    ContextLayer,
)
from ...contracts.context_manifest import ContextPolicyIdentity


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


@dataclass(frozen=True)
class ContextPolicy:
    policy_id: str = "krelcode-context"
    version: str = "1"
    category_order: tuple[str, ...] = CATEGORY_ORDER
    retrieval_scopes: tuple[str, ...] = (
        "global", "tenant", "organization", "project", "user",
        "session", "task",
    )
    tool_disclosure: str = "progressive"
    compression_strategy: str = "structured_summary+budgeted_rehydration"
    sensitive_content_policy: str = "instruction-evidence-separation"
    budget_policy: ContextBudgetPolicy = field(
        default_factory=ContextBudgetPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "policy_id", str(self.policy_id or "").strip())
        object.__setattr__(
            self, "version", str(self.version or "").strip())
        if not self.policy_id or not self.version:
            raise ValueError("ContextPolicy 缺少 policy_id/version")
        object.__setattr__(
            self,
            "category_order",
            tuple(dict.fromkeys(str(item) for item in self.category_order)),
        )
        object.__setattr__(
            self,
            "retrieval_scopes",
            tuple(dict.fromkeys(str(item) for item in self.retrieval_scopes)),
        )
        for name in (
            "tool_disclosure", "compression_strategy",
            "sensitive_content_policy",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "category_order": list(self.category_order),
            "retrieval_scopes": list(self.retrieval_scopes),
            "tool_disclosure": self.tool_disclosure,
            "compression_strategy": self.compression_strategy,
            "sensitive_content_policy": self.sensitive_content_policy,
            "budget_policy": {
                "max_tokens": self.budget_policy.max_tokens,
                "category_caps": dict(self.budget_policy.category_caps),
                "required_categories": list(
                    self.budget_policy.required_categories),
            },
        }

    def identity(self) -> ContextPolicyIdentity:
        payload = self.to_dict()
        digest = hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
        return ContextPolicyIdentity(
            policy_id=self.policy_id,
            version=self.version,
            digest=digest,
            configuration=payload,
        )


__all__ = [
    "ContextBudgetDecision",
    "ContextBudgetPolicy",
    "ContextPolicy",
]
