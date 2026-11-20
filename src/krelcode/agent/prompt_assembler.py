"""The single request-level prompt assembly path.

Prompt fragments are collected from the fragment registry and ContextSources,
ordered into the fixed L0-L12 hierarchy, and converted once into the exact
provider-bound messages.  ``ContextAssembly`` remains available as the
source-attribution compatibility view consumed by ``/context`` and session
checkpoints, but production requests no longer assemble messages independently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Optional, Sequence, Tuple

from ..core.messages import Message
from ..llm.base import (
    SYSTEM_BLOCK_CHANNEL_EVIDENCE,
    SYSTEM_BLOCK_CHANNEL_INSTRUCTION,
    SYSTEM_PROMPT_BLOCKS_META_KEY,
    EvidenceBlock,
    EvidenceChannel,
    SystemPromptBlock,
    SystemPromptBlocks,
)
from .attachments import (
    ContextAttachment,
    ContextAttachmentKind,
    context_attachment_fragments,
)
from .context_assembler import ContextAssembler
from .context_source import (
    CATEGORY_MEMORY,
    CATEGORY_RUNTIME,
    CATEGORY_SYSTEM,
    CATEGORY_WORKSPACE,
    TRUST_SYSTEM,
    ContextAssembly,
    ContextLayer,
    ContextRequest,
    ContextSourceIssue,
    ContextTokenContribution,
    default_context_sources,
    prompt_fragment_omission_reason,
    select_prompt_fragments,
)
from .evidence import EvidenceSource, SourceRole, instruction_source_for
from .fragments import (
    GROUP_WORKSPACE,
    LAYER_L1_BASE_AGENT,
    LAYER_L3_PROJECT_RULES,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L7_EVIDENCE,
    LAYER_L7_MEMORY,
    LAYER_L9_WORKING,
    LAYER_L10_HISTORY,
    LAYER_L11_USER_REQUEST,
    LAYER_L12_REMINDER,
    LAYER_L8_ENVIRONMENT,
    LAYER_ORDER,
    PROMPT_LAYERS,
    STABILITY_ORDER,
    STABILITY_SESSION,
    STABILITY_TURN,
    SYSTEM_PROMPT_LAYERS,
    TRUST_DATA,
    TRUST_MANAGED,
    TRUST_PROJECT,
    TRUST_UNTRUSTED as FRAGMENT_TRUST_UNTRUSTED,
    TRUST_USER,
    PromptCacheBoundary,
    PromptFragment,
    PromptFragmentRegistry,
    PromptSnapshot,
    WORKSPACE_CLOSE,
    WORKSPACE_OPEN,
)
from .tokens import estimate_text_tokens
from .prompt_plan import PromptOmissionSnapshot
from .tool_playbooks import (
    DEFAULT_TOOL_PLAYBOOK_CATALOG,
    ToolPlaybookCatalog,
)


@dataclass(frozen=True)
class PromptAssemblyRequest:
    """Inputs for one exact model request."""

    context_request: ContextRequest
    prompt_snapshot: Optional[PromptSnapshot] = None
    tools: Tuple[Mapping[str, object], ...] = ()
    per_turn_reminders: Tuple[str, ...] = ()
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "instruction_blocks", tuple(self.instruction_blocks))
        object.__setattr__(self, "evidence_channel", self.evidence_channel)

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
        ]


class PromptAssembler:
    """Own the only path from fragments/context to ``ProviderMessages``."""

    def __init__(
        self,
        context_assembler: Optional[ContextAssembler] = None,
        *,
        registry_provider: Optional[
            Callable[[], Optional[PromptFragmentRegistry]]
        ] = None,
        tool_playbook_catalog: Optional[ToolPlaybookCatalog] = None,
    ) -> None:
        self.context_assembler = context_assembler or ContextAssembler(
            default_context_sources())
        self.registry_provider = registry_provider
        self.tool_playbook_catalog = (
            tool_playbook_catalog or DEFAULT_TOOL_PLAYBOOK_CATALOG)
        self.engine = str(
            getattr(self.context_assembler, "engine", "v3") or "v3")

    async def assemble(self, request: PromptAssemblyRequest) -> PromptPlan:
        context_request = request.context_request
        snapshot = (
            request.prompt_snapshot
            or context_request.prompt_snapshot
            or self._snapshot_from_registry()
        )
        prompt_fragments, omitted, scope_filter = self._prompt_fragments(
            snapshot, context_request)
        prompt_omissions = _prompt_omission_details(
            snapshot, context_request, omitted)
        playbook_fragments = self.tool_playbook_catalog.fragments_for_tools(
            _advertised_tool_names(
                request.tools, context_request.tool_pool))
        attachment_fragments, attachment_omissions = _select_attachment_fragments(
            request.attachments, context_request)
        if not scope_filter and any(
            item.reason == "file-explanation-scope"
            for item in attachment_omissions
        ):
            scope_filter = "file_explanation"
        all_omissions = _dedupe_omissions(
            (*prompt_omissions, *attachment_omissions))
        all_omitted_ids = tuple(item.id for item in all_omissions)

        # ContextSources contribute only the dynamic layers here.  The prompt
        # source is disabled by using an empty snapshot/system prompt so the
        # exact same source implementations can be reused without a second
        # prompt rendering path.
        dynamic_request = replace(
            context_request,
            system_prompt="",
            system_provider_meta=None,
            prompt_snapshot=None,
        )
        dynamic_assembly = await self.context_assembler.assemble(dynamic_request)
        dynamic_fragments = tuple(
            _fragment_from_context_layer(layer)
            for layer in dynamic_assembly.layers
        )

        current_user = _current_user_message(context_request.history)
        conversation_fragments = _conversation_fragments(
            context_request.history,
            current_user,
            request.per_turn_reminders,
        )
        fragments = _ordered_fragments(
            tuple(
                _annotate_fragment_authority(fragment)
                for fragment in (
                    *prompt_fragments,
                    *playbook_fragments,
                    *attachment_fragments,
                    *dynamic_fragments,
                    *conversation_fragments,
                )
            )
        )

        prompt_fragments_for_request = tuple(
            fragment for fragment in fragments
            if fragment.layer in SYSTEM_PROMPT_LAYERS
            or fragment.layer == LAYER_L12_REMINDER
        )
        instruction_fragments = tuple(
            fragment for fragment in prompt_fragments_for_request
            if _fragment_channel(fragment) == SYSTEM_BLOCK_CHANNEL_INSTRUCTION
        )
        evidence_fragments = tuple(
            fragment for fragment in prompt_fragments_for_request
            if _fragment_channel(fragment) == SYSTEM_BLOCK_CHANNEL_EVIDENCE
        )
        system_blocks = _build_system_prompt_blocks(instruction_fragments)
        evidence_blocks = _build_evidence_blocks(evidence_fragments)
        system_text = system_blocks.merged_text
        stable_offset = system_blocks.cache_control_boundary
        stable_ids = tuple(
            block.id for block in system_blocks.stable_system_blocks)
        stable_set = set(stable_ids)
        volatile_ids = tuple(
            fragment.id for fragment in fragments
            if fragment.id not in stable_set
        )
        cache = PromptCacheBoundary(
            offset=stable_offset,
            stable_tokens=estimate_text_tokens(system_blocks.stable_text),
            stable_fragments=stable_ids,
            volatile_fragments=volatile_ids,
            stable_hash=system_blocks.stable_hash,
            full_hash=hashlib.sha256(system_text.encode("utf-8")).hexdigest(),
            dynamic_hash=system_blocks.dynamic_hash,
        )
        layers = _layer_plans(fragments)
        registry_and_attachment_fragments = (
            *prompt_fragments,
            *playbook_fragments,
            *attachment_fragments,
        )
        contributions = _token_contributions(
            registry_and_attachment_fragments,
            dynamic_assembly.token_contributions,
        )
        assembly = _compatibility_assembly(
            prompt_fragments=registry_and_attachment_fragments,
            dynamic_assembly=dynamic_assembly,
            system_text=system_text,
            cache=cache,
            contributions=contributions,
            fragment_count=len(fragments),
            omitted=all_omitted_ids,
            scope_filter=scope_filter,
            snapshot=snapshot,
            engine=self.engine,
        )
        history = tuple(
            message for message in context_request.history
            if message.role != "system"
        )
        return PromptPlan(
            fragments=fragments,
            layers=layers,
            system_text=system_text,
            history=history,
            current_user=current_user,
            cache=cache,
            generation=max(
                int(getattr(snapshot, "generation", 0) or 0),
                0,
            ),
            engine=self.engine,
            system_blocks=system_blocks,
            evidence_blocks=evidence_blocks,
            token_contributions=contributions,
            omitted_fragments=all_omitted_ids,
            omitted_details=all_omissions,
            issues=dynamic_assembly.issues,
            context_assembly=assembly,
        )

    def provider_messages(
        self,
        plan: PromptPlan,
        *,
        tools: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> ProviderMessages:
        messages = list(plan.history)
        evidence_blocks = plan.evidence_blocks
        if plan.system_text:
            provider_meta = dict(plan.provider_meta or {})
            if plan.system_blocks is not None:
                provider_meta[SYSTEM_PROMPT_BLOCKS_META_KEY] = plan.system_blocks
            system = Message.system(
                plan.system_text,
                provider_meta=provider_meta or None,
            )
            messages.insert(0, system)
        anchor_index = _evidence_anchor_index(messages, plan.current_user)
        evidence_channel = EvidenceChannel(
            blocks=evidence_blocks,
            anchor_message_index=anchor_index,
        )
        return ProviderMessages(
            messages=tuple(messages),
            tools=tuple(tools or ()),
            cache_boundary=plan.cache,
            system_blocks=plan.system_blocks,
            instruction_blocks=plan.instruction_blocks,
            evidence_channel=evidence_channel,
        )

    def _snapshot_from_registry(self) -> Optional[PromptSnapshot]:
        if self.registry_provider is None:
            return None
        registry = self.registry_provider()
        return registry.snapshot() if registry is not None else None

    def _prompt_fragments(
        self,
        snapshot: Optional[PromptSnapshot],
        request: ContextRequest,
    ) -> tuple[
        Tuple[PromptFragment, ...], Tuple[str, ...], str
    ]:
        if snapshot is not None:
            if self.engine == "v3":
                return select_prompt_fragments(snapshot, request)
            return tuple(snapshot.fragments), (), ""
        text = (request.system_prompt or "").strip()
        if not text:
            return (), (), ""
        return (
            PromptFragment(
                id="legacy:system",
                source="system",
                title="Legacy system prompt",
                content=text,
                group="system",
                layer=LAYER_L1_BASE_AGENT,
                priority=0,
                cacheable=False,
                compactable=False,
                toggleable=False,
                scope="managed",
                stability=STABILITY_SESSION,
                trust=TRUST_MANAGED,
            ),
        ), (), ""


def _fragment_from_context_layer(layer: ContextLayer) -> PromptFragment:
    layer_id, stability, trust = _context_layer_classification(layer)
    return PromptFragment(
        id=layer.id,
        source=layer.source,
        title=layer.origin or layer.id,
        content=layer.content,
        group=(
            GROUP_WORKSPACE
            if layer.category == CATEGORY_WORKSPACE
            else "runtime"
        ),
        priority=layer.priority,
        cacheable=(
            layer.cacheable
            and stability != STABILITY_TURN
            and trust not in {TRUST_DATA, FRAGMENT_TRUST_UNTRUSTED}
        ),
        compactable=layer.compactable,
        toggleable=False,
        metadata=dict(layer.metadata),
        scope=_scope_for_trust(trust),
        layer=layer_id,
        stability=stability,
        trust=trust,
    )


_INSTRUCTION_LAYERS = frozenset(PROMPT_LAYERS[:7])


def _annotate_fragment_authority(fragment: PromptFragment) -> PromptFragment:
    """Attach explicit authority metadata at the single assembly boundary."""

    metadata = dict(fragment.metadata)
    if fragment.layer in _INSTRUCTION_LAYERS:
        instruction_source = instruction_source_for(
            fragment.trust,
            source=fragment.source,
            origin=str(metadata.get("origin", "")),
        )
        if instruction_source is not None:
            metadata.update({
                "source_role": SourceRole.INSTRUCTION.value,
                "instruction_source": instruction_source.value,
                "instruction": True,
            })
            return replace(fragment, metadata=metadata)
    if fragment.layer == LAYER_L7_EVIDENCE:
        evidence_source = _evidence_source_for(fragment, metadata)
        metadata.update({
            "source_role": SourceRole.EVIDENCE.value,
            "evidence_type": evidence_source.value,
            "instruction": False,
            "requires_validation": True,
        })
        return replace(fragment, metadata=metadata)
    return fragment


def _evidence_source_for(
    fragment: PromptFragment,
    metadata: Mapping[str, object],
) -> EvidenceSource:
    explicit = str(metadata.get("evidence_type", "") or "").strip().lower()
    if explicit:
        try:
            return EvidenceSource(explicit)
        except ValueError:
            pass
    marker = " ".join((
        fragment.id,
        fragment.source,
        str(metadata.get("evidence_origin", "") or ""),
        str(metadata.get("origin", "") or ""),
    )).lower()
    if "memory" in marker:
        return EvidenceSource.MEMORY
    if "mcp" in marker:
        return EvidenceSource.MCP
    if "web" in marker:
        return EvidenceSource.WEB
    if "tool" in marker:
        return EvidenceSource.TOOL_RESULT
    return EvidenceSource.RETRIEVED_DOCUMENT


def _context_layer_classification(
    layer: ContextLayer,
) -> tuple[str, str, str]:
    metadata = layer.metadata
    prompt_layer = str(metadata.get("prompt_layer", "") or "")
    prompt_stability = str(metadata.get("prompt_stability", "") or "")
    prompt_trust = str(metadata.get("prompt_trust", "") or "")
    if (
        prompt_layer in LAYER_ORDER
        and prompt_stability in STABILITY_ORDER
        and prompt_trust in {
            TRUST_MANAGED, TRUST_USER, TRUST_PROJECT,
            TRUST_DATA, FRAGMENT_TRUST_UNTRUSTED,
        }
    ):
        return prompt_layer, prompt_stability, prompt_trust
    if (
        str(metadata.get("source_role", "")).strip().lower() == "evidence"
        or bool(metadata.get("evidence_type"))
    ):
        evidence_trust = (
            FRAGMENT_TRUST_UNTRUSTED
            if layer.trust != TRUST_SYSTEM else TRUST_DATA
        )
        return LAYER_L7_EVIDENCE, STABILITY_TURN, evidence_trust
    if layer.id == "memory:scoped" or layer.source in {
        "memory", "memory_retrieval",
    }:
        return LAYER_L7_EVIDENCE, STABILITY_TURN, FRAGMENT_TRUST_UNTRUSTED
    if layer.id == "workspace:directory-rules":
        return LAYER_L3_PROJECT_RULES, STABILITY_SESSION, TRUST_PROJECT
    if layer.source == "tool_directory":
        return LAYER_L5_MODE_CONTRACT, STABILITY_TURN, TRUST_PROJECT
    if layer.source in {
        "turn_intent", "working_context", "runtime:working-context",
    }:
        return LAYER_L9_WORKING, STABILITY_TURN, TRUST_DATA
    if layer.source == "runtime_hooks":
        return LAYER_L12_REMINDER, STABILITY_TURN, FRAGMENT_TRUST_UNTRUSTED
    if layer.category == CATEGORY_MEMORY:
        return LAYER_L7_EVIDENCE, STABILITY_TURN, FRAGMENT_TRUST_UNTRUSTED
    if layer.category == CATEGORY_WORKSPACE:
        return LAYER_L8_ENVIRONMENT, STABILITY_TURN, TRUST_PROJECT
    if layer.category == CATEGORY_RUNTIME:
        return LAYER_L9_WORKING, STABILITY_TURN, TRUST_DATA
    return LAYER_L1_BASE_AGENT, STABILITY_SESSION, TRUST_MANAGED


def _scope_for_trust(trust: str) -> str:
    if trust == TRUST_MANAGED:
        return "managed"
    if trust == TRUST_USER:
        return "user"
    if trust == TRUST_PROJECT:
        return "project"
    return "session"


def _select_attachment_fragments(
    attachments: Sequence[ContextAttachment],
    request: ContextRequest,
) -> Tuple[
    Tuple[PromptFragment, ...], Tuple[PromptOmissionSnapshot, ...]
]:
    """Apply request-scope rules without mutating the attachment store."""
    selected: list[ContextAttachment] = []
    omitted: list[PromptOmissionSnapshot] = []
    for attachment in attachments:
        if (
            request.intent is not None
            and request.intent.is_file_explanation
            and attachment.kind in {
                ContextAttachmentKind.ENVIRONMENT_SNAPSHOT,
                ContextAttachmentKind.ENVIRONMENT_DELTA,
            }
        ):
            omitted.append(PromptOmissionSnapshot(
                id=attachment.id,
                source=f"attachment:{attachment.kind.value}",
                scope=attachment.scope,
                layer=attachment.layer,
                stability=attachment.stability,
                trust=attachment.trust,
                tokens=attachment.tokens,
                reason="file-explanation-scope",
            ))
            continue
        if attachment.content.strip():
            selected.append(attachment)
        else:
            omitted.append(PromptOmissionSnapshot(
                id=attachment.id,
                source=f"attachment:{attachment.kind.value}",
                scope=attachment.scope,
                layer=attachment.layer,
                stability=attachment.stability,
                trust=attachment.trust,
                reason="empty-content",
            ))
    return context_attachment_fragments(selected), tuple(omitted)


def _prompt_omission_details(
    snapshot: Optional[PromptSnapshot],
    request: ContextRequest,
    omitted_ids: Sequence[str],
) -> tuple[PromptOmissionSnapshot, ...]:
    if snapshot is None or not omitted_ids:
        return ()
    omitted = set(omitted_ids)
    details: list[PromptOmissionSnapshot] = []
    for fragment in snapshot.fragments:
        if fragment.id not in omitted:
            continue
        details.append(PromptOmissionSnapshot(
            id=fragment.id,
            source=fragment.source,
            scope=fragment.scope,
            layer=fragment.layer,
            stability=fragment.stability,
            trust=fragment.trust,
            tokens=fragment.tokens,
            reason=prompt_fragment_omission_reason(fragment, request),
        ))
    return tuple(details)


def _dedupe_omissions(
    omissions: Sequence[PromptOmissionSnapshot],
) -> tuple[PromptOmissionSnapshot, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[PromptOmissionSnapshot] = []
    for omission in omissions:
        key = (omission.id, omission.reason)
        if key in seen:
            continue
        seen.add(key)
        result.append(omission)
    return tuple(result)


def _current_user_message(history: Sequence[Message]) -> Optional[Message]:
    for message in reversed(history):
        if message.role == "user":
            return message
    return None


def _advertised_tool_names(
    tools: Sequence[Mapping[str, object]],
    tool_pool: object,
) -> Tuple[str, ...]:
    """Return tools whose schemas are actually visible in this model request."""
    names = {
        str(tool.get("name", "")).strip()
        for tool in tools
        if str(tool.get("name", "")).strip()
    }
    if names:
        return tuple(sorted(names))
    if tool_pool is None:
        return ()
    advertised_names = getattr(tool_pool, "advertised_names", None)
    if callable(advertised_names):
        return tuple(str(name) for name in advertised_names())
    return ()


def _conversation_fragments(
    history: Sequence[Message],
    current_user: Optional[Message],
    reminders: Sequence[str],
) -> Tuple[PromptFragment, ...]:
    fragments: list[PromptFragment] = []
    if history:
        digest = hashlib.sha256(
            "\n".join(
                f"{message.role}:{message.content or ''}"
                for message in history
            ).encode("utf-8")
        ).hexdigest()
        fragments.append(PromptFragment(
            id="conversation:history",
            source="conversation",
            title="Recent conversation history",
            content=digest,
            group="runtime",
            priority=0,
            cacheable=False,
            compactable=True,
            toggleable=False,
            metadata={
                "messages": len(history),
                "digest": digest,
                "content_omitted": True,
            },
            scope="session",
            layer=LAYER_L10_HISTORY,
            stability=STABILITY_TURN,
            trust=TRUST_USER,
        ))
    if current_user is not None:
        fragments.append(PromptFragment(
            id="conversation:current-user",
            source="conversation",
            title="Current user request",
            content=current_user.content or "[image attachment]",
            group="runtime",
            priority=10,
            cacheable=False,
            compactable=False,
            toggleable=False,
            metadata={"images": len(current_user.images or ())},
            scope="session",
            layer=LAYER_L11_USER_REQUEST,
            stability=STABILITY_TURN,
            trust=TRUST_USER,
        ))
    for index, raw in enumerate(reminders, 1):
        text = str(raw).strip()
        if not text:
            continue
        fragments.append(PromptFragment(
            id=f"reminder:turn:{index}",
            source="per_turn_reminder",
            title=f"Per-turn reminder {index}",
            content=text,
            group="runtime",
            priority=100 + index,
            cacheable=False,
            compactable=True,
            toggleable=False,
            metadata={"index": index},
            scope="session",
            layer=LAYER_L12_REMINDER,
            stability=STABILITY_TURN,
            trust=TRUST_MANAGED,
        ))
    return tuple(fragments)


def _ordered_fragments(
    fragments: Sequence[PromptFragment],
) -> Tuple[PromptFragment, ...]:
    seen: set[str] = set()
    deduplicated: list[PromptFragment] = []
    for fragment in fragments:
        if fragment.id in seen:
            continue
        seen.add(fragment.id)
        deduplicated.append(fragment)
    return tuple(sorted(deduplicated, key=_fragment_plan_sort_key))


def _fragment_plan_sort_key(fragment: PromptFragment) -> tuple:
    return (
        LAYER_ORDER.get(fragment.layer, len(PROMPT_LAYERS)),
        STABILITY_ORDER.get(fragment.stability, len(STABILITY_ORDER)),
        fragment.priority,
        fragment.source,
        fragment.id,
    )


def _build_system_prompt_blocks(
    fragments: Sequence[PromptFragment],
) -> SystemPromptBlocks:
    stable: list[SystemPromptBlock] = []
    dynamic: list[SystemPromptBlock] = []
    stable_open = True
    for fragment in fragments:
        content = _wire_fragment_content(fragment)
        if not content:
            continue
        channel = _fragment_channel(fragment)
        metadata = _prompt_block_metadata(fragment)
        block = SystemPromptBlock(
            id=fragment.id,
            text=content,
            source=fragment.source,
            stability=fragment.stability,
            trust=fragment.trust,
            cacheable=fragment.cacheable,
            channel=channel,
            scope=fragment.scope,
            evidence_type=(
                str(metadata.get("evidence_type", "") or "")
                if channel == SYSTEM_BLOCK_CHANNEL_EVIDENCE else ""),
            requires_validation=(
                bool(metadata.get("requires_validation", True))
                if channel == SYSTEM_BLOCK_CHANNEL_EVIDENCE else False),
            metadata=metadata,
        )
        if stable_open and fragment.cacheable:
            stable.append(block)
        else:
            stable_open = False
            dynamic.append(block)
    blocks = SystemPromptBlocks(
        stable_system_blocks=tuple(stable),
        dynamic_system_blocks=tuple(dynamic),
    )
    return blocks


def _build_evidence_blocks(
    fragments: Sequence[PromptFragment],
) -> Tuple[EvidenceBlock, ...]:
    """Build the independent evidence channel from L7 fragments."""
    blocks: list[EvidenceBlock] = []
    for fragment in fragments:
        content = fragment.content.strip()
        if not content:
            continue
        metadata = _prompt_block_metadata(fragment)
        blocks.append(EvidenceBlock(
            id=fragment.id,
            text=content,
            source=fragment.source,
            evidence_type=str(metadata.get("evidence_type", "") or ""),
            scope=fragment.scope,
            origin=str(metadata.get("evidence_origin", "") or ""),
            trust=fragment.trust,
            requires_validation=bool(
                metadata.get("requires_validation", True)),
            metadata=metadata,
        ))
    return tuple(blocks)


def _evidence_anchor_index(
    messages: Sequence[Message],
    current_user: Optional[Message],
) -> int:
    """Return the transient user-message index that owns this turn's evidence."""
    if current_user is None:
        return -1
    for index, message in enumerate(messages):
        if message is current_user:
            return index
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "user" and message == current_user:
            return index
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return -1


_PROMPT_BLOCK_METADATA_KEYS = frozenset({
    "source_role",
    "instruction",
    "instruction_source",
    "evidence_type",
    "evidence_sources",
    "evidence_ids",
    "evidence_count",
    "evidence_origin",
    "requires_validation",
    "confidence_min",
    "confidence_max",
    "prompt_section",
    "prompt_section_required",
    "prompt_profile",
    "prompt_profile_requested",
    "prompt_profile_match",
    "prompt_provider",
    "prompt_model",
    "base_prompt_hash",
    "tool_playbook",
    "tool_playbook_version",
    "tool_playbook_hash",
    "tool_playbook_content_hash",
    "tool_playbook_tools",
    "tool_playbook_active_tools",
    "mcp_server",
    "mcp_servers",
    "attachment_id",
    "attachment_kind",
    "attachment_revision",
    "contract_version",
    "contract_hash",
    "run_mode",
    "permission_mode",
    "approval_profile",
    "approval_precedence",
    "approval_requires_approver",
    "approval_auto_approves_routine",
    "approval_fail_closed",
    "side_effect_policy",
    "plan_behavior",
    "sandbox_enabled",
    "sandbox_backend",
    "sandbox_network_allowed",
    "sandbox_network_controlled",
    "sandbox_os_enforced",
    "sandbox_shell_write_scope",
    "sandbox_file_write_scope",
    "protected_paths_enforced",
    "sensitive_reads_protected",
    "tool_allowlist_active",
    "mcp_allowlist_active",
    "mcp_scope",
    "subagent",
    "subagent_definition",
    "tool_total_count",
    "eligible_tool_count",
    "excluded_tool_count",
    "read_only_tool_count",
    "mutating_tool_count",
    "local_read_only_tool_count",
    "loaded_tool_count",
    "advertised_tool_count",
    "tool_exclusion_categories",
    "tool_exposure_hash",
})


def _fragment_channel(fragment: PromptFragment) -> str:
    metadata = fragment.metadata
    if (
        str(metadata.get("source_role", "") or "").strip().lower()
        == SourceRole.EVIDENCE.value
        or bool(metadata.get("evidence_type"))
        or fragment.layer == LAYER_L7_EVIDENCE
    ):
        return SYSTEM_BLOCK_CHANNEL_EVIDENCE
    return SYSTEM_BLOCK_CHANNEL_INSTRUCTION


def _prompt_block_metadata(fragment: PromptFragment) -> dict[str, object]:
    """Keep only inspectable provenance; never copy arbitrary rule/tool data."""
    return {
        key: value
        for key, value in fragment.metadata.items()
        if key in _PROMPT_BLOCK_METADATA_KEYS
    }


def _render_system_fragments(
    fragments: Sequence[PromptFragment],
) -> tuple[str, int, tuple[str, ...]]:
    text = ""
    stable_end = 0
    stable_open = True
    stable_ids: list[str] = []
    for fragment in fragments:
        content = _wire_fragment_content(fragment)
        if not content:
            continue
        separator = "\n\n" if text else ""
        text += separator + content
        if stable_open and fragment.cacheable:
            stable_end = len(text)
            stable_ids.append(fragment.id)
        else:
            stable_open = False
    return text, stable_end, tuple(stable_ids)


def _wire_fragment_content(fragment: PromptFragment) -> str:
    content = fragment.content.strip()
    if not content:
        return ""
    if (
        fragment.group == GROUP_WORKSPACE
        and "<workspace_context>" not in content
    ):
        return f"{WORKSPACE_OPEN}\n{content}\n{WORKSPACE_CLOSE}"
    return content


def _layer_plans(
    fragments: Sequence[PromptFragment],
) -> Tuple[PromptLayerPlan, ...]:
    grouped: list[PromptLayerPlan] = []
    for layer in PROMPT_LAYERS:
        members = tuple(
            fragment for fragment in fragments if fragment.layer == layer)
        if not members:
            continue
        grouped.append(PromptLayerPlan(
            layer=layer,
            fragments=members,
            system=(
                layer in SYSTEM_PROMPT_LAYERS
                or layer == LAYER_L12_REMINDER
            ),
        ))
    return tuple(grouped)


def _token_contributions(
    prompt_fragments: Sequence[PromptFragment],
    dynamic_contributions: Sequence[ContextTokenContribution],
) -> Tuple[ContextTokenContribution, ...]:
    merged: dict[tuple[str, str], int] = {}
    for fragment in prompt_fragments:
        category = _fragment_category(fragment)
        key = (fragment.source, category)
        merged[key] = merged.get(key, 0) + fragment.tokens
    for contribution in dynamic_contributions:
        key = (contribution.source, contribution.category)
        merged[key] = merged.get(key, 0) + max(0, contribution.tokens)
    return tuple(
        ContextTokenContribution(source, category, tokens)
        for (source, category), tokens in sorted(merged.items())
    )


def _fragment_category(fragment: PromptFragment) -> str:
    if fragment.group == GROUP_WORKSPACE:
        return CATEGORY_WORKSPACE
    if fragment.layer == LAYER_L8_ENVIRONMENT:
        return CATEGORY_WORKSPACE
    if fragment.layer == LAYER_L7_MEMORY:
        return CATEGORY_MEMORY
    if fragment.layer in {
        LAYER_L9_WORKING,
        LAYER_L10_HISTORY,
        LAYER_L11_USER_REQUEST,
        LAYER_L12_REMINDER,
    }:
        return CATEGORY_RUNTIME
    return CATEGORY_SYSTEM


def _compatibility_assembly(
    *,
    prompt_fragments: Sequence[PromptFragment],
    dynamic_assembly: ContextAssembly,
    system_text: str,
    cache: PromptCacheBoundary,
    contributions: Sequence[ContextTokenContribution],
    fragment_count: int,
    omitted: Sequence[str],
    scope_filter: str,
    snapshot: Optional[PromptSnapshot],
    engine: str,
) -> ContextAssembly:
    prompt_text, prompt_stable, _ = _render_system_fragments(tuple(
        fragment for fragment in prompt_fragments
        if fragment.layer in SYSTEM_PROMPT_LAYERS
        and _fragment_channel(fragment) == SYSTEM_BLOCK_CHANNEL_INSTRUCTION
    ))
    metadata: dict[str, object] = {
        "fragment_count": len(prompt_fragments),
        "engine": engine,
        "included_fragment_ids": tuple(
            fragment.id for fragment in prompt_fragments),
    }
    if omitted:
        metadata["excluded_fragments"] = tuple(omitted)
        metadata["scope_filter"] = scope_filter
    prompt_layer = ContextLayer(
        id="source:prompt",
        source="prompt",
        category=CATEGORY_SYSTEM,
        content=prompt_text,
        trust=TRUST_SYSTEM,
        origin="prompt_registry",
        priority=0,
        cacheable=prompt_stable > 0,
        stable_prefix_chars=prompt_stable,
        compactable=False,
        metadata=metadata,
        token_contributions=tuple(
            ContextTokenContribution(
                fragment.source,
                _fragment_category(fragment),
                fragment.tokens,
            )
            for fragment in prompt_fragments
        ),
    )
    layers = ((prompt_layer,) if prompt_text else ()) + dynamic_assembly.layers
    sources = {layer.source for layer in layers}
    provider_meta = (
        {"prompt_cache_boundaries": [cache.offset]}
        if cache.offset > 0 else None
    )
    return ContextAssembly(
        text=system_text,
        layers=layers,
        stable_prefix_chars=cache.offset,
        stable_hash=cache.stable_hash,
        full_hash=cache.full_hash,
        token_contributions=tuple(contributions),
        engine=engine,
        source_count=len(sources),
        fragment_count=fragment_count,
        issues=dynamic_assembly.issues,
        provider_meta=provider_meta,
        prompt_snapshot=snapshot,
        dynamic_hash=cache.dynamic_hash,
    )


__all__ = [
    "PromptAssembler",
    "PromptAssemblyRequest",
    "PromptLayerPlan",
    "PromptPlan",
    "ProviderMessages",
]
