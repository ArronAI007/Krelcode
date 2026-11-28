"""Stable context assembly contracts shared by sources and projects."""

from __future__ import annotations

import hashlib
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
    source_type: str = ""
    source_id: str = ""
    scope: str = ""
    version: str = ""
    permission_basis: str = ""
    selected_reason: str = ""
    transform: str = ""
    content_hash: str = ""
    priority: int = 100
    cacheable: bool = False
    stable_prefix_chars: Optional[int] = None
    compactable: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)
    token_contributions: Tuple[ContextTokenContribution, ...] = ()

    def __post_init__(self) -> None:
        metadata = dict(self.metadata)
        object.__setattr__(self, "source_type", str(
            self.source_type
            or metadata.get("source_type")
            or self.category
            or "unknown"
        ))
        object.__setattr__(self, "source_id", str(
            self.source_id
            or metadata.get("source_id")
            or self.origin
            or self.source
        ))
        object.__setattr__(self, "scope", str(
            self.scope
            or metadata.get("scope")
            or metadata.get("rule_scope")
            or "session"
        ))
        object.__setattr__(self, "version", str(
            self.version
            or metadata.get("version")
            or metadata.get("prompt_release_id")
            or metadata.get("tool_playbook_version")
            or ""
        ))
        object.__setattr__(self, "permission_basis", str(
            self.permission_basis
            or metadata.get("permission_basis")
            or metadata.get("approval_profile")
            or ""
        ))
        object.__setattr__(self, "selected_reason", str(
            self.selected_reason
            or metadata.get("selected_reason")
            or metadata.get("selection_reason")
            or ""
        ))
        object.__setattr__(self, "transform", str(
            self.transform
            or metadata.get("transform")
            or "verbatim"
        ))
        object.__setattr__(
            self,
            "content_hash",
            str(
                self.content_hash
                or hashlib.sha256(self.content.encode("utf-8")).hexdigest()
            ),
        )

    @property
    def tokens(self) -> int:
        return estimate_text_tokens(self.content)

    def effective_token_contributions(self) -> Tuple[ContextTokenContribution, ...]:
        if self.token_contributions:
            return self.token_contributions
        return (ContextTokenContribution(
            self.source, self.category, self.tokens),)
