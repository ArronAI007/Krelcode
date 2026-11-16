"""Pluggable context sources and their immutable contribution contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Mapping, Optional, Protocol, Sequence, Tuple

from ..core.messages import Message
from .fragments import (
    GROUP_RUNTIME,
    GROUP_WORKSPACE,
    PromptSnapshot,
)
from .intent import TurnIntent
from .tokens import estimate_text_tokens
from .tool_pool import TurnToolPool

CATEGORY_SYSTEM = "system"
CATEGORY_WORKSPACE = "workspace"
CATEGORY_MEMORY = "memory"
CATEGORY_RUNTIME = "runtime"
CATEGORY_ORDER = (
    CATEGORY_SYSTEM,
    CATEGORY_WORKSPACE,
    CATEGORY_MEMORY,
    CATEGORY_RUNTIME,
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


@dataclass(frozen=True)
class ContextSourceIssue:
    """A source failure or invalid contribution; error text is intentionally bounded."""

    source: str
    error: str


@dataclass(frozen=True)
class ContextRequest:
    """Everything a context source may inspect for one model request."""

    turn_id: str
    step: int
    history: Tuple[Message, ...]
    workspace_root: Path
    system_prompt: str = ""
    system_provider_meta: Optional[Mapping[str, object]] = None
    prompt_snapshot: Optional[PromptSnapshot] = None
    tool_pool: Optional[TurnToolPool] = None
    intent: Optional[TurnIntent] = None
    runtime_context: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextAssembly:
    """Final, provider-bound system context plus source attribution."""

    text: str
    layers: Tuple[ContextLayer, ...]
    stable_prefix_chars: int
    stable_hash: str
    full_hash: str
    token_contributions: Tuple[ContextTokenContribution, ...]
    source_count: int = 0
    fragment_count: int = 0
    issues: Tuple[ContextSourceIssue, ...] = ()
    provider_meta: Optional[Mapping[str, object]] = None
    prompt_snapshot: Optional[PromptSnapshot] = None

    @property
    def stable_prefix(self) -> str:
        return self.text[:self.stable_prefix_chars]

    def system_message(self) -> Message:
        return Message.system(self.text, provider_meta=dict(self.provider_meta or {}) or None)

    def messages_for(self, history: Sequence[Message]) -> list[Message]:
        """Apply this assembly to history without mutating persistent messages."""
        messages = list(history)
        if not self.text:
            return messages
        message = self.system_message()
        if messages and messages[0].role == "system":
            messages[0] = message
        else:
            messages.insert(0, message)
        return messages


ContextSourceResult = Sequence[ContextLayer] | Awaitable[Sequence[ContextLayer]]


class ContextSource(Protocol):
    """A named collector of ordered context layers."""

    name: str

    def collect(self, request: ContextRequest) -> ContextSourceResult:
        """Return layers now, or an awaitable that resolves to layers."""
        ...


class PromptSnapshotContextSource:
    """Expose the PromptFragmentRegistry snapshot as source-attributed context."""

    name = "prompt"

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        snapshot = request.prompt_snapshot
        text = request.system_prompt or (snapshot.text if snapshot else "")
        if not text:
            return ()
        stable_chars = 0
        fragment_count = 0
        contributions = []
        if snapshot is not None and text == snapshot.text:
            stable_chars = snapshot.cache.offset
            fragment_count = len(snapshot.fragments)
            for fragment in snapshot.fragments:
                contributions.append(ContextTokenContribution(
                    source=fragment.source,
                    category=_fragment_category(fragment),
                    tokens=fragment.tokens,
                ))
        else:
            contributions.append(ContextTokenContribution(
                source="system",
                category=CATEGORY_SYSTEM,
                tokens=estimate_text_tokens(text),
            ))
        return (ContextLayer(
            id="source:prompt",
            source=self.name,
            category=CATEGORY_SYSTEM,
            content=text,
            priority=0,
            cacheable=stable_chars > 0,
            stable_prefix_chars=stable_chars,
            compactable=False,
            metadata={"fragment_count": fragment_count},
            token_contributions=tuple(contributions),
        ),)


class ToolDirectoryContextSource:
    """Render the current turn tool directory without exposing schemas here."""

    name = "tool_directory"

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        if request.tool_pool is None:
            return ()
        content = request.tool_pool.system_hint()
        if not content:
            return ()
        return (ContextLayer(
            id="source:tool-directory",
            source=self.name,
            category=CATEGORY_RUNTIME,
            content=content,
            priority=100,
            compactable=True,
            token_contributions=(ContextTokenContribution(
                "runtime:tools", CATEGORY_RUNTIME,
                estimate_text_tokens(content)),),
        ),)


class TurnIntentContextSource:
    """Render the deterministic file/action intent hint for this turn."""

    name = "turn_intent"

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        if request.intent is None:
            return ()
        content = request.intent.system_hint(request.workspace_root)
        if not content:
            return ()
        return (ContextLayer(
            id="source:turn-intent",
            source=self.name,
            category=CATEGORY_RUNTIME,
            content=content,
            priority=200,
            compactable=True,
            token_contributions=(ContextTokenContribution(
                "runtime:intent", CATEGORY_RUNTIME,
                estimate_text_tokens(content)),),
        ),)


class RuntimeHookContextSource:
    """Expose SessionStart/Compact hook context as explicit runtime layers."""

    name = "runtime_hooks"

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        layers = []
        for index, raw in enumerate(request.runtime_context, 1):
            text = str(raw).strip()
            if not text:
                continue
            content = f"<runtime_context>\n{text}\n</runtime_context>"
            layers.append(ContextLayer(
                id=f"source:runtime-hook:{index}",
                source=self.name,
                category=CATEGORY_RUNTIME,
                content=content,
                priority=300 + index,
                compactable=True,
                metadata={"index": index, "origin": "hook"},
                token_contributions=(ContextTokenContribution(
                    f"runtime:hook:{index}", CATEGORY_RUNTIME,
                    estimate_text_tokens(content)),),
            ))
        return tuple(layers)


def default_context_sources() -> Tuple[ContextSource, ...]:
    return (
        PromptSnapshotContextSource(),
        ToolDirectoryContextSource(),
        TurnIntentContextSource(),
        RuntimeHookContextSource(),
    )


def _fragment_category(fragment) -> str:
    if fragment.group == GROUP_WORKSPACE:
        return CATEGORY_WORKSPACE
    if fragment.source == "memory" or fragment.id.startswith("memory:"):
        return CATEGORY_MEMORY
    if fragment.group == GROUP_RUNTIME:
        return CATEGORY_RUNTIME
    return CATEGORY_SYSTEM
