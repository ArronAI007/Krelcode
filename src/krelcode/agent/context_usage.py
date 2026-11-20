"""Derive observable token accounting from the exact next model request."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..core.context_usage import ContextUsageSnapshot
from ..core.messages import Message
from ..llm.base import PreparedLLMRequest
from .context_assembler import ContextAssembly
from .fragments import GROUP_RUNTIME, GROUP_WORKSPACE, PromptSnapshot
from .tokens import (
    estimate_history_tokens,
    estimate_text_tokens,
)

_CATEGORIES = (
    "system",
    "tool_schema",
    "workspace",
    "memory",
    "history",
    "runtime",
    "overhead",
)


def estimate_tool_schema_tokens(
    specs: Sequence[Mapping[str, object]],
) -> int:
    """Estimate the provider-facing JSON schema payload size."""
    if not specs:
        return 0
    try:
        rendered = json.dumps(
            list(specs), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = repr(list(specs))
    return estimate_text_tokens(rendered)


def build_context_usage_snapshot(
    messages: Sequence[Message],
    *,
    turn_id: str,
    step: int,
    tool_specs: Sequence[Mapping[str, object]] = (),
    prompt_snapshot: Optional[PromptSnapshot] = None,
    base_system_text: str = "",
    runtime_hints: Iterable[str] = (),
    context_assembly: Optional[ContextAssembly] = None,
    budget_tokens: Optional[int] = None,
    model_context_window: Optional[int] = None,
    dropped_items: Sequence[str] = (),
    prepared_request: Optional[PreparedLLMRequest] = None,
    provider_name: str = "",
    output_reserve_tokens: Optional[int] = None,
) -> ContextUsageSnapshot:
    """Build a compact snapshot for the messages about to be sent.

    ``messages`` must be the final, provider-bound list.  The calculation uses
    the same heuristic estimator as context compression, then reconciles small
    per-fragment rounding differences so all exposed token categories add up to
    ``estimated_input_tokens`` exactly.
    """
    message_list = list(messages)
    schema_tokens = estimate_tool_schema_tokens(tool_specs)
    heuristic_input = estimate_history_tokens(message_list) + schema_tokens
    estimated = (
        prepared_request.request_body_tokens
        if prepared_request is not None else heuristic_input
    )

    contributions: List[Tuple[str, str, int]] = []
    if context_assembly is not None:
        contributions.extend(
            (item.source, item.category, item.tokens)
            for item in context_assembly.token_contributions
        )
    else:
        if prompt_snapshot is not None:
            for fragment in prompt_snapshot.fragments:
                category = _fragment_category(fragment)
                contributions.append((fragment.source, category, fragment.tokens))
        elif base_system_text:
            contributions.append((
                "system", "system", estimate_text_tokens(base_system_text)))

        hint_text = "\n\n".join(
            str(item) for item in runtime_hints if str(item).strip())
        if hint_text:
            contributions.append((
                "runtime:turn", "runtime", estimate_text_tokens(hint_text)))

    history = (
        message_list[1:]
        if message_list and message_list[0].role == "system"
        else message_list
    )
    if history:
        contributions.append(("history", "history", estimate_history_tokens(history)))
    if schema_tokens:
        contributions.append(("tool_schema", "tool_schema", schema_tokens))

    raw_total = sum(max(0, tokens) for _, _, tokens in contributions)
    if raw_total <= estimated:
        contributions.append(("overhead", "overhead", estimated - raw_total))
    normalized = _normalize_contributions(contributions, estimated)

    category_tokens = {category: 0 for category in _CATEGORIES}
    source_tokens: Dict[str, int] = defaultdict(int)
    for (source, category), tokens in normalized.items():
        category_tokens[category] += tokens
        source_tokens[source] += tokens

    if context_assembly is not None:
        stable_tokens = estimate_text_tokens(context_assembly.stable_prefix)
        fragments = ()
        fragment_count = context_assembly.fragment_count
        cache_boundary_chars = context_assembly.stable_prefix_chars
        stable_hash = context_assembly.stable_hash
        dynamic_hash = str(
            getattr(context_assembly, "dynamic_hash", "") or "")
        full_hash = context_assembly.full_hash
        source_count = context_assembly.source_count
    else:
        cache = getattr(prompt_snapshot, "cache", None)
        stable_tokens = int(getattr(cache, "stable_tokens", 0) or 0)
        fragments = tuple(getattr(prompt_snapshot, "fragments", ()) or ())
        fragment_count = len(fragments)
        cache_boundary_chars = int(getattr(cache, "offset", 0) or 0)
        stable_hash = str(getattr(cache, "stable_hash", "") or "")
        dynamic_hash = str(getattr(cache, "dynamic_hash", "") or "")
        full_hash = str(getattr(cache, "full_hash", "") or "")
        source_count = 0
    prepared_segments = (
        prepared_request.cache_segments if prepared_request is not None else ())
    if prepared_request is not None:
        if prepared_request.stable_cache_chars:
            cache_boundary_chars = prepared_request.stable_cache_chars
        if prepared_request.stable_cache_hash:
            stable_hash = prepared_request.stable_cache_hash
        if prepared_request.dynamic_cache_hash:
            dynamic_hash = prepared_request.dynamic_cache_hash
        if stable_tokens == 0:
            stable_tokens = sum(
                segment.tokens for segment in prepared_segments
                if segment.kind == "system")
    stable_tokens = min(stable_tokens, estimated)
    window = model_context_window
    if output_reserve_tokens is not None:
        reserve = max(0, int(output_reserve_tokens))
    elif prepared_request is not None and prepared_request.output_limit_tokens > 0:
        reserve = prepared_request.output_limit_tokens
    elif window is not None and budget_tokens is not None:
        reserve = max(0, int(window) - int(budget_tokens))
    else:
        reserve = 0
    input_fits = (
        (budget_tokens is None or estimated <= int(budget_tokens))
        and (window is None or estimated + reserve <= int(window))
    )
    tool_cacheable_tokens = sum(
        segment.tokens for segment in prepared_segments
        if segment.kind == "tools")
    message_cacheable_tokens = sum(
        segment.tokens for segment in prepared_segments
        if segment.kind == "messages")
    return ContextUsageSnapshot(
        turn_id=turn_id,
        step=step,
        estimated_input_tokens=estimated,
        context_engine=str(getattr(context_assembly, "engine", "") or ""),
        budget_tokens=budget_tokens,
        model_context_window=window,
        output_reserve_tokens=reserve,
        system_tokens=category_tokens["system"],
        tool_schema_tokens=category_tokens["tool_schema"],
        workspace_tokens=category_tokens["workspace"],
        memory_tokens=category_tokens["memory"],
        history_tokens=category_tokens["history"],
        runtime_tokens=category_tokens["runtime"],
        overhead_tokens=category_tokens["overhead"],
        stable_tokens=stable_tokens,
        volatile_tokens=max(0, estimated - stable_tokens),
        cache_boundary_chars=cache_boundary_chars,
        stable_hash=stable_hash,
        dynamic_hash=dynamic_hash,
        full_hash=full_hash,
        stable_system_block_count=(
            prepared_request.stable_system_block_count
            if prepared_request is not None else 0),
        dynamic_system_block_count=(
            prepared_request.dynamic_system_block_count
            if prepared_request is not None else 0),
        evidence_block_count=(
            prepared_request.evidence_block_count
            if prepared_request is not None else 0),
        evidence_hash=(
            prepared_request.evidence_hash
            if prepared_request is not None else ""),
        cache_control_boundary=(
            prepared_request.cache_control_boundary
            if prepared_request is not None
            else cache_boundary_chars),
        tool_count=len(tool_specs),
        fragment_count=fragment_count,
        source_count=source_count,
        source_tokens=dict(sorted(source_tokens.items())),
        dropped_items=tuple(str(item) for item in dropped_items),
        provider=provider_name or (
            prepared_request.provider if prepared_request is not None else ""),
        wire_fidelity=(
            prepared_request.wire_fidelity
            if prepared_request is not None else "estimated"),
        cache_mode=(
            prepared_request.cache_mode
            if prepared_request is not None else "estimated"),
        cache_requested_mode=(
            prepared_request.cache_negotiation.requested_mode
            if prepared_request is not None
            and prepared_request.cache_negotiation is not None else ""),
        cache_negotiation_reason=(
            prepared_request.cache_negotiation.reason
            if prepared_request is not None
            and prepared_request.cache_negotiation is not None else ""),
        cache_supported_modes=(
            prepared_request.cache_negotiation.supported_modes
            if prepared_request is not None
            and prepared_request.cache_negotiation is not None else ()),
        cache_fallback=(
            prepared_request.cache_negotiation.fallback
            if prepared_request is not None
            and prepared_request.cache_negotiation is not None else False),
        cache_max_breakpoints=(
            prepared_request.cache_negotiation.max_breakpoints
            if prepared_request is not None
            and prepared_request.cache_negotiation is not None else 0),
        request_body_bytes=(
            prepared_request.body_bytes if prepared_request is not None else 0),
        request_body_tokens=(
            prepared_request.request_body_tokens
            if prepared_request is not None else 0),
        wire_overhead_tokens=max(0, estimated - heuristic_input),
        request_digest=(
            prepared_request.payload_digest
            if prepared_request is not None else ""),
        cache_segment_count=len(prepared_segments),
        cache_control_segment_count=sum(
            1 for segment in prepared_segments if segment.cache_control),
        tool_cacheable_tokens=tool_cacheable_tokens,
        message_cacheable_tokens=message_cacheable_tokens,
        output_limit_tokens=(
            prepared_request.output_limit_tokens
            if prepared_request is not None else 0),
        input_fits=input_fits,
        cache_segments=tuple(prepared_segments),
    )


def _fragment_category(fragment) -> str:
    if fragment.group == GROUP_WORKSPACE:
        return "workspace"
    if fragment.source == "memory" or fragment.id.startswith("memory:"):
        return "memory"
    if fragment.group == GROUP_RUNTIME:
        return "runtime"
    return "system"


def _normalize_contributions(
    contributions: List[Tuple[str, str, int]],
    total: int,
) -> Dict[Tuple[str, str], int]:
    """Round a token partition to ``total`` without negative values.

    Independent heuristic estimates can round up when parts are measured
    separately.  Largest-remainder normalization keeps the public breakdown
    exact while preserving relative weights.
    """
    if total <= 0:
        return {}
    raw: Dict[Tuple[str, str], int] = defaultdict(int)
    for source, category, tokens in contributions:
        raw[(source, category)] += max(0, int(tokens))
    current = sum(raw.values())
    if current == 0:
        return {("overhead", "overhead"): total}
    if current == total:
        return dict(raw)
    floors: Dict[Tuple[str, str], int] = {
        key: (value * total) // current for key, value in raw.items()
    }
    remaining = total - sum(floors.values())
    ranked = sorted(
        raw,
        key=lambda key: (
            -((raw[key] * total) % current),
            key[0],
            key[1],
        ),
    )
    for key in ranked[:remaining]:
        floors[key] += 1
    return floors
