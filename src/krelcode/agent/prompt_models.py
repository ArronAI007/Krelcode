"""Immutable prompt request, plan, and provider-neutral message models."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from ..core.messages import Message
from ..llm.base import (
    EvidenceBlock,
    EvidenceChannel,
    SystemPromptBlock,
    SystemPromptBlocks,
)
from ..tools.prompt import ToolPromptSpec
from .attachments import ContextAttachment
from .context_contracts import ContextTokenContribution
from .context_source import (
    ContextAssembly,
    ContextRequest,
    ContextSourceIssue,
)
from .fragments import PromptCacheBoundary, PromptFragment, PromptSnapshot
from .prompt_conflicts import PromptConflictReport
from .prompt_omission import PromptOmissionSnapshot
from .reminders import SelectedReminder


@dataclass(frozen=True)
class PromptAssemblyRequest:
    """Inputs for one exact model request."""

    context_request: ContextRequest
    prompt_snapshot: Optional[PromptSnapshot] = None
    tools: Tuple[object, ...] = ()
    per_turn_reminders: Tuple[str | SelectedReminder, ...] = ()
    attachments: Tuple[ContextAttachment, ...] = ()


@dataclass(frozen=True)
class ProviderMessages:
    """Provider-neutral request produced by the one prompt assembly path."""

    messages: Tuple[Message, ...]
    tools: Tuple[Mapping[str, object], ...] = ()
    cache_boundary: Optional[PromptCacheBoundary] = None
    system_blocks: Optional[SystemPromptBlocks] = None
    instruction_blocks: Tuple[SystemPromptBlock, ...] = ()
    evidence_channel: EvidenceChannel = EvidenceChannel()
    tool_prompt_specs: Tuple[ToolPromptSpec, ...] = ()
    conflict_report: Optional[PromptConflictReport] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "instruction_blocks", tuple(self.instruction_blocks))
        object.__setattr__(self, "evidence_channel", self.evidence_channel)
        object.__setattr__(
            self, "tool_prompt_specs", tuple(self.tool_prompt_specs))

    @property
    def evidence_blocks(self) -> Tuple[EvidenceBlock, ...]:
        return self.evidence_channel.blocks

    @property
    def stable_hash(self) -> str:
        if self.system_blocks is None:
            return ""
        return self.system_blocks.stable_hash

    @property
    def dynamic_hash(self) -> str:
        if self.system_blocks is None:
            return ""
        return self.system_blocks.dynamic_hash

    @property
    def full_hash(self) -> str:
        if self.system_blocks is None:
            return ""
        return hashlib.sha256(
            self.system_blocks.merged_text.encode("utf-8")
        ).hexdigest()

    @property
    def instruction_hash(self) -> str:
        return self.system_blocks.instruction_hash if self.system_blocks else ""

    @property
    def evidence_hash(self) -> str:
        return self.evidence_channel.hash

    def channel_record(self) -> Mapping[str, object]:
        """Content-free observability for the provider-neutral channels."""
        return {
            "instruction_blocks": [
                block.as_record() for block in self.instruction_blocks],
            "evidence_blocks": [
                block.as_record() for block in self.evidence_blocks],
            "instruction_hash": self.instruction_hash,
            "evidence_hash": self.evidence_hash,
            "evidence_channel": self.evidence_channel.as_record(),
            "cache_boundary": (
                self.cache_boundary.offset
                if self.cache_boundary is not None else 0),
            "tool_prompt_specs": [
                spec.as_record() for spec in self.tool_prompt_specs],
            "conflict_report": (
                self.conflict_report.as_dict()
                if self.conflict_report is not None else None),
        }

    @property
    def provider_meta(self) -> Optional[Mapping[str, object]]:
        metadata: dict[str, object] = {}
        if self.cache_boundary is not None and self.cache_boundary.enabled:
            metadata["prompt_cache_boundaries"] = [self.cache_boundary.offset]
        return metadata or None


@dataclass(frozen=True)
class PromptLayerPlan:
    """One fixed L0-L12 band after ordering and validation."""

    layer: str
    fragments: Tuple[PromptFragment, ...]
    system: bool


@dataclass(frozen=True)
class PromptPlan:
    """Validated prompt plan before it is serialized for a provider."""

    fragments: Tuple[PromptFragment, ...]
    layers: Tuple[PromptLayerPlan, ...]
    system_text: str
    history: Tuple[Message, ...]
    current_user: Optional[Message]
    cache: PromptCacheBoundary
    generation: int
    engine: str
    system_blocks: Optional[SystemPromptBlocks] = None
    evidence_blocks: Tuple[EvidenceBlock, ...] = ()
    token_contributions: Tuple[ContextTokenContribution, ...] = ()
    omitted_fragments: Tuple[str, ...] = ()
    omitted_details: Tuple[PromptOmissionSnapshot, ...] = ()
    issues: Tuple[ContextSourceIssue, ...] = ()
    context_assembly: Optional[ContextAssembly] = None
    tool_prompt_specs: Tuple[ToolPromptSpec, ...] = ()
    conflict_report: Optional[PromptConflictReport] = None

    @property
    def cache_boundary(self) -> int:
        return self.cache.offset

    @property
    def stable_prefix(self) -> str:
        return self.system_text[:self.cache.offset]

    @property
    def stable_system_blocks(self) -> Tuple[SystemPromptBlock, ...]:
        return (self.system_blocks.stable_system_blocks
                if self.system_blocks is not None else ())

    @property
    def dynamic_system_blocks(self) -> Tuple[SystemPromptBlock, ...]:
        return (self.system_blocks.dynamic_system_blocks
                if self.system_blocks is not None else ())

    @property
    def instruction_blocks(self) -> Tuple[SystemPromptBlock, ...]:
        return (self.system_blocks.instruction_blocks
                if self.system_blocks is not None else ())

    @property
    def instruction_hash(self) -> str:
        return (self.system_blocks.instruction_hash
                if self.system_blocks is not None else "")

    @property
    def evidence_hash(self) -> str:
        return EvidenceChannel(blocks=self.evidence_blocks).hash

    @property
    def dynamic_hash(self) -> str:
        return self.cache.dynamic_hash

    @property
    def stable_hash(self) -> str:
        return self.cache.stable_hash

    @property
    def full_hash(self) -> str:
        return self.cache.full_hash

    @property
    def provider_meta(self) -> Optional[Mapping[str, object]]:
        if not self.cache.enabled:
            return None
        return {"prompt_cache_boundaries": [self.cache.offset]}

    def summary_lines(self) -> list[str]:
        stable = len(self.cache.stable_fragments)
        volatile = len(self.cache.volatile_fragments)
        return [
            f"prompt plan {self.engine or 'v3'} · fragment {len(self.fragments)} 个"
            f"（稳定 {stable} / 动态 {volatile}）",
            f"provider channels: instruction {len(self.instruction_blocks)}"
            f" · evidence {len(self.evidence_blocks)}",
            (
                f"cache boundary: {self.cache.offset} chars"
                f" · stable hash {self.cache.stable_hash[:12]}"
                if self.cache.enabled else "cache boundary: 未启用"
            ),
            (
                f"prompt conflicts: {self.conflict_report.conflict_count}"
                f" · unresolved {self.conflict_report.unresolved_count}"
                if self.conflict_report is not None else
                "prompt conflicts: analyzer 未运行"
            ),
        ]


__all__ = [
    "PromptAssemblyRequest",
    "PromptLayerPlan",
    "PromptPlan",
    "ProviderMessages",
]
