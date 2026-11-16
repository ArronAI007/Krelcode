"""Derive observable token accounting from the exact next model request."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..core.context_usage import ContextUsageSnapshot
from ..core.messages import Message
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
    budget_tokens: Optional[int] = None,
    model_context_window: Optional[int] = None,
    dropped_items: Sequence[str] = (),
) -> ContextUsageSnapshot:
    """Build a compact snapshot for the messages about to be sent.

    ``messages`` must be the final, provider-bound list.  The calculation uses
    the same heuristic estimator as context compression, then reconciles small
    per-fragment rounding differences so all exposed token categories add up to
    ``estimated_input_tokens`` exactly.
    """
    message_list = list(messages)
    schema_tokens = estimate_tool_schema_tokens(tool_specs)
    estimated = estimate_history_tokens(message_list) + schema_tokens

    contributions: List[Tuple[str, str, int]] = []
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

    cache = getattr(prompt_snapshot, "cache", None)
    stable_tokens = int(getattr(cache, "stable_tokens", 0) or 0)
    stable_tokens = min(stable_tokens, estimated)
    fragments = tuple(getattr(prompt_snapshot, "fragments", ()) or ())
    window = model_context_window
    reserve = (
        max(0, int(window) - int(budget_tokens))
        if window is not None and budget_tokens is not None else 0
    )
    return ContextUsageSnapshot(
        turn_id=turn_id,
        step=step,
        estimated_input_tokens=estimated,
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
        cache_boundary_chars=int(getattr(cache, "offset", 0) or 0),
        stable_hash=str(getattr(cache, "stable_hash", "") or ""),
        full_hash=str(getattr(cache, "full_hash", "") or ""),
        tool_count=len(tool_specs),
        fragment_count=len(fragments),
        source_tokens=dict(sorted(source_tokens.items())),
        dropped_items=tuple(str(item) for item in dropped_items),
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
