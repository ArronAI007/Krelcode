"""Serializable context-usage contracts shared by agent, session and UI layers.

The snapshot is intentionally derived data: it never contains the assembled
system prompt, message bodies, tool arguments or credentials.  Consumers can
persist or expose it without opening a second prompt/secret leak path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ContextUsageSnapshot:
    """Token accounting for one concrete model request."""

    turn_id: str
    step: int
    estimated_input_tokens: int
    budget_tokens: Optional[int] = None
    model_context_window: Optional[int] = None
    output_reserve_tokens: int = 0
    system_tokens: int = 0
    tool_schema_tokens: int = 0
    workspace_tokens: int = 0
    memory_tokens: int = 0
    history_tokens: int = 0
    runtime_tokens: int = 0
    overhead_tokens: int = 0
    stable_tokens: int = 0
    volatile_tokens: int = 0
    cache_boundary_chars: int = 0
    stable_hash: str = ""
    full_hash: str = ""
    tool_count: int = 0
    fragment_count: int = 0
    source_count: int = 0
    source_tokens: Mapping[str, int] = field(default_factory=dict)
    dropped_items: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_tokens", MappingProxyType(dict(self.source_tokens)))

    @property
    def total_tokens(self) -> int:
        return self.estimated_input_tokens

    @property
    def budget(self) -> Optional[int]:
        return self.budget_tokens

    @property
    def context_window(self) -> Optional[int]:
        return self.model_context_window

    @property
    def usage_ratio(self) -> Optional[float]:
        if not self.budget_tokens:
            return None
        return self.estimated_input_tokens / self.budget_tokens

    @property
    def source_token_map(self) -> Dict[str, int]:
        return dict(self.source_tokens)

    def as_record(self) -> Dict[str, object]:
        """Return a compact JSON-compatible record without prompt content."""
        return {
            "turn_id": self.turn_id,
            "step": self.step,
            "estimated_input_tokens": self.estimated_input_tokens,
            "budget_tokens": self.budget_tokens,
            "model_context_window": self.model_context_window,
            "output_reserve_tokens": self.output_reserve_tokens,
            "system_tokens": self.system_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "workspace_tokens": self.workspace_tokens,
            "memory_tokens": self.memory_tokens,
            "history_tokens": self.history_tokens,
            "runtime_tokens": self.runtime_tokens,
            "overhead_tokens": self.overhead_tokens,
            "stable_tokens": self.stable_tokens,
            "volatile_tokens": self.volatile_tokens,
            "cache_boundary_chars": self.cache_boundary_chars,
            "stable_hash": self.stable_hash,
            "full_hash": self.full_hash,
            "tool_count": self.tool_count,
            "fragment_count": self.fragment_count,
            "source_count": self.source_count,
            "source_tokens": dict(self.source_tokens),
            "dropped_items": list(self.dropped_items),
        }
