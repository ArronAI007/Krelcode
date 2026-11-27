"""Stable context assembly contracts shared by sources and projects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

from krelcode.core.tokens import estimate_text_tokens

CATEGORY_SYSTEM = "system"
CATEGORY_WORKSPACE = "workspace"
CATEGORY_KNOWLEDGE = "knowledge"
CATEGORY_MEMORY = "memory"
CATEGORY_RUNTIME = "runtime"
CATEGORY_ORDER = (
    CATEGORY_SYSTEM,
    CATEGORY_WORKSPACE,
    CATEGORY_KNOWLEDGE,
    CATEGORY_MEMORY,
    CATEGORY_RUNTIME,
)

TRUST_SYSTEM = "system"
TRUST_TRUSTED = "trusted"
TRUST_CONTEXTUAL = "contextual"
TRUST_UNTRUSTED = "untrusted"
CONTEXT_TRUST_LEVELS = (
    TRUST_SYSTEM,
    TRUST_TRUSTED,
    TRUST_CONTEXTUAL,
    TRUST_UNTRUSTED,
)


@dataclass(frozen=True)
class ContextTokenContribution:
    """Token attribution retained even when sources share one rendered layer."""

    source: str
    category: str
    tokens: int


@dataclass(frozen=True)
class ContextLayer:
    """One ordered piece of system context produced by a ContextSource."""

    id: str
    source: str
    category: str
    content: str
    trust: str = TRUST_TRUSTED
    origin: str = ""
    priority: int = 100
    cacheable: bool = False
    stable_prefix_chars: Optional[int] = None
    compactable: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
    token_contributions: Tuple[ContextTokenContribution, ...] = ()

    @property
    def tokens(self) -> int:
        return estimate_text_tokens(self.content)

    def effective_token_contributions(self) -> Tuple[ContextTokenContribution, ...]:
        if self.token_contributions:
            return self.token_contributions
        return (ContextTokenContribution(
            self.source, self.category, self.tokens),)
