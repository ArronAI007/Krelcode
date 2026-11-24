"""Pure prompt ordering, channel, rendering and compatibility helpers."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Mapping, Optional, Sequence, Tuple

from krelcode.core.messages import Message
from krelcode.llm.base import SYSTEM_BLOCK_CHANNEL_INSTRUCTION
from krelcode.tools.prompt import ToolPromptSpec
from krelcode.agent.attachments import (
    ContextAttachment,
    ContextAttachmentKind,
    context_attachment_fragments,
)
from krelcode.agent.context_contracts import (
    CATEGORY_MEMORY,
    CATEGORY_RUNTIME,
    CATEGORY_SYSTEM,
    CATEGORY_WORKSPACE,
    TRUST_SYSTEM,
    ContextLayer,
    ContextTokenContribution,
)
from krelcode.agent.context_source import (
    ContextAssembly,
    ContextRequest,
    prompt_fragment_omission_reason,
)
from krelcode.agent.evidence import EvidenceSource, SourceRole, instruction_source_for
from krelcode.agent.prompt.fragments import (
    GROUP_WORKSPACE,
    LAYER_L1_BASE_AGENT,
    LAYER_L3_PROJECT_RULES,
    LAYER_L5_MODE_CONTRACT,
    LAYER_L7_EVIDENCE,
    LAYER_L7_MEMORY,
    LAYER_L8_ENVIRONMENT,
    LAYER_L9_WORKING,
    LAYER_L10_HISTORY,
    LAYER_L11_USER_REQUEST,
    LAYER_L12_REMINDER,
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
    PromptSnapshot,
)
from krelcode.agent.prompt.prompt_channels import _fragment_channel, _render_system_fragments
from krelcode.agent.prompt.prompt_models import PromptLayerPlan
from krelcode.agent.prompt.prompt_plan import PromptOmissionSnapshot
from krelcode.agent.reminders import SelectedReminder


def _empty_dynamic_assembly(engine: str) -> ContextAssembly:
    empty_hash = hashlib.sha256(b"").hexdigest()
    return ContextAssembly(
        text="",
        layers=(),
        stable_prefix_chars=0,
        stable_hash=empty_hash,
        full_hash=empty_hash,
        dynamic_hash=empty_hash,
        token_contributions=(),
        engine=engine,
    )


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
    tools: Sequence[object],
    tool_pool: object,
) -> Tuple[str, ...]:
    """Return tools whose schemas are actually visible in this model request."""
    names = {
        str(
            tool.get("name", "")
            if isinstance(tool, Mapping)
            else getattr(tool, "name", "")
        ).strip()
        for tool in tools
        if str(
            tool.get("name", "")
            if isinstance(tool, Mapping)
            else getattr(tool, "name", "")
        ).strip()
    }
    if names:
        return tuple(sorted(names))
    if tool_pool is None:
        return ()
    advertised_names = getattr(tool_pool, "advertised_names", None)
    if callable(advertised_names):
        return tuple(str(name) for name in advertised_names())
    return ()


def _normalize_tool_prompt_specs(
    tools: Sequence[object],
) -> Tuple[ToolPromptSpec, ...]:
    normalized: list[ToolPromptSpec] = []
    seen: set[str] = set()
    for tool in tools:
        if isinstance(tool, ToolPromptSpec):
            spec = tool
        elif isinstance(tool, Mapping):
            spec = ToolPromptSpec.from_mapping(tool)
        else:
            spec = ToolPromptSpec.from_tool(tool)
        if spec.name in seen:
            raise ValueError(f"ToolPromptSpec 名称重复: {spec.name}")
        seen.add(spec.name)
        normalized.append(spec)
    return tuple(sorted(normalized, key=lambda item: item.name))


def _conversation_fragments(
    history: Sequence[Message],
    current_user: Optional[Message],
    reminders: Sequence[str | SelectedReminder],
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
        if isinstance(raw, SelectedReminder):
            spec = raw.spec
            fragments.append(PromptFragment(
                id=f"reminder:{spec.id}",
                source=f"reminder:{spec.source}",
                title=f"Per-turn reminder: {spec.id}",
                content=spec.content.strip(),
                group="runtime",
                priority=spec.priority,
                cacheable=False,
                compactable=True,
                toggleable=False,
                metadata={
                    "reminder_id": spec.id,
                    "reminder_source": spec.source,
                    "reminder_trigger": spec.trigger,
                    "selection_reason": raw.reason,
                    "requested": raw.requested,
                    "catalog_trust": spec.trust,
                    **dict(spec.metadata),
                },
                scope="session",
                layer=LAYER_L12_REMINDER,
                stability=STABILITY_TURN,
                trust=spec.trust,
            ))
            continue
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


def _fragment_plan_sort_key(
    fragment: PromptFragment,
) -> Tuple[int, int, int, str, str]:
    return (
        LAYER_ORDER.get(fragment.layer, len(PROMPT_LAYERS)),
        STABILITY_ORDER.get(fragment.stability, len(STABILITY_ORDER)),
        fragment.priority,
        fragment.source,
        fragment.id,
    )


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
