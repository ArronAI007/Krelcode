"""Pluggable context sources and their immutable contribution contracts."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING, Awaitable, Mapping, Optional, Protocol, Sequence, Tuple,
)

from ..core.messages import Message
from .fragments import (
    GROUP_RUNTIME,
    GROUP_WORKSPACE,
    PromptFragment,
    PromptFragmentRegistry,
    PromptSnapshot,
)
from .intent import TurnIntent
from .rules import RuleBundle, RuleLoader, extract_rule_targets
from .tokens import estimate_text_tokens
from .tool_pool import TurnToolPool

if TYPE_CHECKING:
    from .working_context import WorkingContextSnapshot

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
    working_context: Optional["WorkingContextSnapshot"] = None
    relevant_memory: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextAssembly:
    """Final, provider-bound system context plus source attribution."""

    text: str
    layers: Tuple[ContextLayer, ...]
    stable_prefix_chars: int
    stable_hash: str
    full_hash: str
    token_contributions: Tuple[ContextTokenContribution, ...]
    engine: str = ""
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
        metadata: dict[str, object] = {}
        if snapshot is not None and text == snapshot.text:
            stable_chars = snapshot.cache.offset
            fragment_count = len(snapshot.fragments)
            for fragment in snapshot.fragments:
                contributions.append(ContextTokenContribution(
                    source=fragment.source,
                    category=_fragment_category(fragment),
                    tokens=fragment.tokens,
                ))
                if fragment.source == "memory" or fragment.id.startswith("memory:"):
                    raw_ids = fragment.metadata.get("entry_ids", ())
                    if isinstance(raw_ids, (list, tuple, set)):
                        metadata["memory_entry_ids"] = [
                            str(item) for item in raw_ids]
                    raw_scopes = fragment.metadata.get("scopes", {})
                    if isinstance(raw_scopes, dict):
                        metadata["memory_scope_counts"] = {
                            str(key): max(0, int(value))
                            for key, value in raw_scopes.items()
                        }
                    metadata["memory_tokens"] = fragment.tokens
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
            trust=TRUST_SYSTEM,
            origin="prompt_registry",
            priority=0,
            cacheable=stable_chars > 0,
            stable_prefix_chars=stable_chars,
            compactable=False,
            metadata={"fragment_count": fragment_count, **metadata},
            token_contributions=tuple(contributions),
        ),)


class V3PromptSnapshotContextSource(PromptSnapshotContextSource):
    """Render the prompt registry with v3 trust and intent boundaries.

    Memory and dynamic Working Context are deliberately collected by separate
    sources.  For a high-confidence single-file explanation request, volatile
    git/tree workspace fragments are also omitted so an explicit file scope
    cannot balloon into an unrelated repository scan.
    """

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        snapshot = request.prompt_snapshot
        if snapshot is None:
            return super().collect(request)

        omitted: list[str] = []
        selected: list[PromptFragment] = []
        for fragment in snapshot.fragments:
            if fragment.source == "memory" or fragment.id.startswith("memory:"):
                omitted.append(fragment.id)
                continue
            if (
                request.intent is not None
                and request.intent.is_file_explanation
                and fragment.id in {"workspace:git", "workspace:tree"}
            ):
                omitted.append(fragment.id)
                continue
            selected.append(fragment)
        effective = (
            snapshot if len(selected) == len(snapshot.fragments)
            else PromptFragmentRegistry(selected).snapshot()
        )
        if not effective.text:
            return ()
        metadata: dict[str, object] = {
            "fragment_count": len(effective.fragments),
            "engine": "v3",
        }
        if omitted:
            metadata["excluded_fragments"] = tuple(omitted)
            metadata["scope_filter"] = (
                "file_explanation"
                if request.intent is not None
                and request.intent.is_file_explanation
                else "trust_separation"
            )
        if request.intent is not None and request.intent.is_file_explanation:
            metadata["intent"] = request.intent.mode
            metadata["targets"] = request.intent.labels(request.workspace_root)
        contributions = tuple(
            ContextTokenContribution(
                source=fragment.source,
                category=_fragment_category(fragment),
                tokens=fragment.tokens,
            )
            for fragment in effective.fragments
        )
        return (ContextLayer(
            id="source:prompt",
            source=self.name,
            category=CATEGORY_SYSTEM,
            content=effective.text,
            trust=TRUST_SYSTEM,
            origin="prompt_registry",
            priority=0,
            cacheable=effective.cache.offset > 0,
            stable_prefix_chars=effective.cache.offset,
            compactable=False,
            metadata=metadata,
            token_contributions=contributions,
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
            trust=TRUST_TRUSTED,
            origin="turn_tool_pool",
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
            trust=TRUST_TRUSTED,
            origin="turn_intent",
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
            safe = html.escape(text, quote=False)
            content = (
                '<runtime_context role="data-not-instructions" '
                f'trust="untrusted" origin="hook:{index}">\n'
                f"{safe}\n</runtime_context>"
            )
            layers.append(ContextLayer(
                id=f"source:runtime-hook:{index}",
                source=self.name,
                category=CATEGORY_RUNTIME,
                content=content,
                trust=TRUST_UNTRUSTED,
                origin=f"hook:{index}",
                priority=300 + index,
                compactable=True,
                metadata={"index": index, "origin": "hook"},
                token_contributions=(ContextTokenContribution(
                    f"runtime:hook:{index}", CATEGORY_RUNTIME,
                    estimate_text_tokens(content)),),
            ))
        return tuple(layers)


class RetrievedMemoryContextSource:
    """Expose task-filtered memory retrieved during resume/fork.

    Unlike the broad memory fragment this layer contains only entries selected
    from the checkpoint's last task query. It is marked as untrusted runtime
    data so model-generated knowledge can never become an instruction layer.
    """

    name = "memory_retrieval"

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        layers = []
        for index, raw in enumerate(request.relevant_memory, 1):
            text = str(raw).strip()
            if not text:
                continue
            layers.append(ContextLayer(
                id=f"runtime:memory-retrieval:{index}",
                source=self.name,
                category=CATEGORY_RUNTIME,
                content=text,
                trust=TRUST_UNTRUSTED,
                origin="resume_memory_query",
                priority=250 + index,
                cacheable=False,
                compactable=True,
                metadata={"index": index, "origin": "resume_query"},
                token_contributions=(ContextTokenContribution(
                    f"runtime:memory:{index}", CATEGORY_RUNTIME,
                    estimate_text_tokens(text)),),
            ))
        return tuple(layers)


class MemoryContextSource:
    """Expose scoped memory as untrusted knowledge, never a system instruction."""

    name = "memory"

    def __init__(self, store) -> None:
        self.store = store

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        if self.store is None:
            return ()
        content, selected = self.store.context_view()
        if not content:
            return ()
        scope_counts = {
            scope: sum(
                1 for entry in selected
                if str(getattr(entry.scope, "value", entry.scope)) == scope
            )
            for scope in ("user", "project", "session")
        }
        return (ContextLayer(
            id="memory:scoped",
            source=self.name,
            category=CATEGORY_MEMORY,
            content=content,
            trust=TRUST_UNTRUSTED,
            origin="scoped_memory",
            priority=80,
            cacheable=False,
            compactable=True,
            metadata={
                "memory_entry_ids": tuple(entry.id for entry in selected),
                "memory_scope_counts": scope_counts,
                "entries": len(selected),
                "memory_tokens": estimate_text_tokens(content),
            },
            token_contributions=(ContextTokenContribution(
                "memory:scoped", CATEGORY_MEMORY,
                estimate_text_tokens(content)),),
        ),)



class DirectoryRulesContextSource:
    """Load nested AGENTS.md/KRELCODE.md rules only for current-turn targets."""

    name = "directory_rules"

    def __init__(self, loader: RuleLoader) -> None:
        self.loader = loader
        self.last_bundle = RuleBundle(content="")

    def collect(self, request: ContextRequest) -> Sequence[ContextLayer]:
        targets = extract_rule_targets(
            request.workspace_root,
            intent=request.intent,
            history=request.history,
        )
        bundle = self.loader.load_directory(targets)
        self.last_bundle = bundle
        if not bundle.content:
            return ()
        content = bundle.content
        sources = tuple(
            source.display_path for source in bundle.sources if not source.imported)
        return (ContextLayer(
            id="workspace:directory-rules",
            source=self.name,
            category=CATEGORY_WORKSPACE,
            content=content,
            trust=TRUST_TRUSTED,
            origin="directory_rule_files",
            priority=15,
            cacheable=False,
            compactable=True,
            metadata={
                "rule_files": sources,
                "rule_import_count": bundle.import_count,
                "rule_issue_count": len(bundle.issues),
                "rule_targets": tuple(str(path) for path in targets),
                "truncated": bundle.truncated,
            },
            token_contributions=(ContextTokenContribution(
                "workspace:directory-rules", CATEGORY_WORKSPACE,
                estimate_text_tokens(content)),),
        ),)


def default_context_sources(
    rule_loader: Optional[RuleLoader] = None,
    *,
    working_context_store=None,
    engine: str = "v3",
    memory_store=None,
) -> Tuple[ContextSource, ...]:
    normalized_engine = str(engine or "v3").strip().lower()
    prompt_source: ContextSource = (
        V3PromptSnapshotContextSource()
        if normalized_engine == "v3" else PromptSnapshotContextSource()
    )
    sources: list[ContextSource] = [prompt_source]
    if rule_loader is not None:
        sources.append(DirectoryRulesContextSource(rule_loader))
    if normalized_engine == "v3" and memory_store is not None:
        sources.append(MemoryContextSource(memory_store))
    sources.extend((
        RetrievedMemoryContextSource(),
        ToolDirectoryContextSource(),
        TurnIntentContextSource(),
        RuntimeHookContextSource(),
    ))
    if working_context_store is not None:
        from .working_context import WorkingContextSource
        sources.append(WorkingContextSource(working_context_store))
    return tuple(sources)


def _fragment_category(fragment) -> str:
    if fragment.group == GROUP_WORKSPACE:
        return CATEGORY_WORKSPACE
    if fragment.source == "memory" or fragment.id.startswith("memory:"):
        return CATEGORY_MEMORY
    if fragment.group == GROUP_RUNTIME:
        return CATEGORY_RUNTIME
    return CATEGORY_SYSTEM
