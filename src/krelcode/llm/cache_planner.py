"""Adaptive explicit prompt-cache breakpoint planning.

The planner is intentionally provider-neutral and content-light.  Providers
translate their final request into ordered prefix candidates, the planner picks
at most the negotiated number of useful boundaries, and the adapter applies the
returned indexes to its wire format.

Selection policy (``adaptive-prefix-v1``):

* keep the moving request frontier so the next turn can reuse this prefix;
* keep the latest stable system boundary and the tool boundary when slots allow;
* use remaining slots for evenly distributed historical message checkpoints;
* never exceed the provider breakpoint limit;
* skip plans below the provider/model cache threshold; and
* return deterministic, content-free diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

from .cache import CACHE_MODE_EXPLICIT


CACHE_BREAKPOINT_PLANNER_VERSION = 1
CACHE_BREAKPOINT_STRATEGY = "adaptive-prefix-v1"
CACHE_BREAKPOINT_MAX_SKIPPED_RECORDS = 50

CACHE_BREAKPOINT_KIND_TOOLS = "tools"
CACHE_BREAKPOINT_KIND_SYSTEM = "system"
CACHE_BREAKPOINT_KIND_MESSAGES = "messages"
CACHE_BREAKPOINT_KINDS = (
    CACHE_BREAKPOINT_KIND_TOOLS,
    CACHE_BREAKPOINT_KIND_SYSTEM,
    CACHE_BREAKPOINT_KIND_MESSAGES,
)

CACHE_BREAKPOINT_STABILITY_STATIC = "static"
CACHE_BREAKPOINT_STABILITY_SESSION = "session"
CACHE_BREAKPOINT_STABILITY_TURN = "turn"
CACHE_BREAKPOINT_STABILITIES = (
    CACHE_BREAKPOINT_STABILITY_STATIC,
    CACHE_BREAKPOINT_STABILITY_SESSION,
    CACHE_BREAKPOINT_STABILITY_TURN,
)

CACHE_BREAKPOINT_REASON_SELECTED = "adaptive_breakpoints_selected"
CACHE_BREAKPOINT_REASON_DISABLED = "cache_control_disabled"
CACHE_BREAKPOINT_REASON_NO_CANDIDATES = "no_cache_candidates"
CACHE_BREAKPOINT_REASON_BELOW_MINIMUM = "below_cache_minimum_tokens"

CACHE_BREAKPOINT_SKIP_BELOW_MINIMUM = "below-minimum"
CACHE_BREAKPOINT_SKIP_INVALID_ORDER = "invalid-prefix-order"
CACHE_BREAKPOINT_SKIP_CAPACITY = "breakpoint-capacity"


def _non_negative_int(value: object) -> int:
    if not isinstance(value, (bool, int, float, str)):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class CacheBreakpointCandidate:
    """One possible prefix boundary in the final provider wire order."""

    id: str
    kind: str
    order: int
    item_index: int
    block_index: int = -1
    cumulative_tokens: int = 0
    incremental_tokens: int = 0
    chars: int = 0
    digest: str = ""
    stability: str = CACHE_BREAKPOINT_STABILITY_SESSION
    anchor: bool = False
    moving: bool = False

    def __post_init__(self) -> None:
        candidate_id = str(self.id or "").strip()
        if not candidate_id:
            raise ValueError("cache breakpoint candidate 必须包含 id")
        kind = str(self.kind or "").strip().lower()
        if kind not in CACHE_BREAKPOINT_KINDS:
            raise ValueError(f"未知 cache breakpoint kind: {kind}")
        stability = str(self.stability or "").strip().lower()
        if stability not in CACHE_BREAKPOINT_STABILITIES:
            raise ValueError(f"未知 cache breakpoint stability: {stability}")
        object.__setattr__(self, "id", candidate_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "order", _non_negative_int(self.order))
        object.__setattr__(self, "item_index", int(self.item_index))
        object.__setattr__(self, "block_index", int(self.block_index))
        object.__setattr__(
            self, "cumulative_tokens", _non_negative_int(self.cumulative_tokens))
        object.__setattr__(
            self, "incremental_tokens", _non_negative_int(self.incremental_tokens))
        object.__setattr__(self, "chars", _non_negative_int(self.chars))
        object.__setattr__(self, "digest", str(self.digest or "").strip())
        object.__setattr__(self, "stability", stability)
        object.__setattr__(self, "anchor", bool(self.anchor))
        object.__setattr__(self, "moving", bool(self.moving))

    def as_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "order": self.order,
            "item_index": self.item_index,
            "block_index": self.block_index,
            "cumulative_tokens": self.cumulative_tokens,
            "incremental_tokens": self.incremental_tokens,
            "chars": self.chars,
            "digest": self.digest,
            "stability": self.stability,
            "anchor": self.anchor,
            "moving": self.moving,
        }


@dataclass(frozen=True)
class CacheBreakpointSkip:
    candidate_id: str
    reason: str

    def as_record(self) -> dict[str, str]:
        return {"id": self.candidate_id, "reason": self.reason}


@dataclass(frozen=True)
class CacheBreakpointPlan:
    """Content-free result consumed by wire adapters and observability."""

    schema_version: int
    mode: str
    strategy: str
    max_breakpoints: int
    minimum_cacheable_tokens: int
    total_prefix_tokens: int
    breakpoints: Tuple[CacheBreakpointCandidate, ...] = ()
    skipped: Tuple[CacheBreakpointSkip, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "breakpoints", tuple(self.breakpoints))
        object.__setattr__(self, "skipped", tuple(self.skipped))
        object.__setattr__(
            self, "max_breakpoints", _non_negative_int(self.max_breakpoints))
        object.__setattr__(
            self, "minimum_cacheable_tokens",
            _non_negative_int(self.minimum_cacheable_tokens))
        object.__setattr__(
            self, "total_prefix_tokens",
            _non_negative_int(self.total_prefix_tokens))
        if len(self.breakpoints) > self.max_breakpoints:
            raise ValueError("cache breakpoint plan 超过 provider 上限")

    @property
    def breakpoint_count(self) -> int:
        return len(self.breakpoints)

    @property
    def selected_prefix_tokens(self) -> int:
        return max(
            (item.cumulative_tokens for item in self.breakpoints),
            default=0,
        )

    @property
    def headroom_breakpoints(self) -> int:
        return max(0, self.max_breakpoints - self.breakpoint_count)

    @property
    def skipped_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.skipped:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return dict(sorted(counts.items()))

    def as_record(self) -> dict[str, object]:
        skipped = self.skipped[:CACHE_BREAKPOINT_MAX_SKIPPED_RECORDS]
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "strategy": self.strategy,
            "reason": self.reason,
            "max_breakpoints": self.max_breakpoints,
            "minimum_cacheable_tokens": self.minimum_cacheable_tokens,
            "total_prefix_tokens": self.total_prefix_tokens,
            "selected_prefix_tokens": self.selected_prefix_tokens,
            "breakpoint_count": self.breakpoint_count,
            "headroom_breakpoints": self.headroom_breakpoints,
            "breakpoints": [item.as_record() for item in self.breakpoints],
            "skipped_count": len(self.skipped),
            "skipped_by_reason": self.skipped_by_reason,
            "skipped": [item.as_record() for item in skipped],
            "skipped_truncated": len(self.skipped) > len(skipped),
        }


class CacheBreakpointPlanner:
    """Deterministic planner for explicit provider cache breakpoints."""

    schema_version = CACHE_BREAKPOINT_PLANNER_VERSION
    strategy = CACHE_BREAKPOINT_STRATEGY

    def plan(
        self,
        candidates: Sequence[CacheBreakpointCandidate],
        *,
        mode: str,
        max_breakpoints: int,
        minimum_cacheable_tokens: int = 0,
    ) -> CacheBreakpointPlan:
        limit = _non_negative_int(max_breakpoints)
        minimum = _non_negative_int(minimum_cacheable_tokens)
        ordered = tuple(sorted(
            (item for item in candidates if isinstance(item, CacheBreakpointCandidate)),
            key=lambda item: (item.order, item.id),
        ))
        total_tokens = max(
            (item.cumulative_tokens for item in ordered), default=0)
        if str(mode or "").strip().lower() != CACHE_MODE_EXPLICIT or limit <= 0:
            return self._empty(
                mode=str(mode or "none"), limit=limit, minimum=minimum,
                total_tokens=total_tokens, reason=CACHE_BREAKPOINT_REASON_DISABLED,
                candidates=ordered,
            )
        if not ordered:
            return self._empty(
                mode=CACHE_MODE_EXPLICIT, limit=limit, minimum=minimum,
                total_tokens=0, reason=CACHE_BREAKPOINT_REASON_NO_CANDIDATES,
            )
        if total_tokens < minimum:
            return self._empty(
                mode=CACHE_MODE_EXPLICIT, limit=limit, minimum=minimum,
                total_tokens=total_tokens,
                reason=CACHE_BREAKPOINT_REASON_BELOW_MINIMUM,
                candidates=ordered,
                skip_reason=CACHE_BREAKPOINT_SKIP_BELOW_MINIMUM,
            )

        eligible: list[CacheBreakpointCandidate] = []
        skipped: list[CacheBreakpointSkip] = []
        previous_tokens = 0
        for candidate in ordered:
            if candidate.cumulative_tokens < previous_tokens:
                skipped.append(CacheBreakpointSkip(
                    candidate.id, CACHE_BREAKPOINT_SKIP_INVALID_ORDER))
                continue
            previous_tokens = candidate.cumulative_tokens
            if candidate.cumulative_tokens < minimum:
                skipped.append(CacheBreakpointSkip(
                    candidate.id, CACHE_BREAKPOINT_SKIP_BELOW_MINIMUM))
                continue
            eligible.append(candidate)
        if not eligible:
            return self._empty(
                mode=CACHE_MODE_EXPLICIT, limit=limit, minimum=minimum,
                total_tokens=total_tokens,
                reason=CACHE_BREAKPOINT_REASON_NO_CANDIDATES,
            )

        selected = self._select(eligible, limit)
        selected_ids = {item.id for item in selected}
        skipped.extend(
            CacheBreakpointSkip(item.id, CACHE_BREAKPOINT_SKIP_CAPACITY)
            for item in eligible if item.id not in selected_ids
        )
        return CacheBreakpointPlan(
            schema_version=self.schema_version,
            mode=CACHE_MODE_EXPLICIT,
            strategy=self.strategy,
            max_breakpoints=limit,
            minimum_cacheable_tokens=minimum,
            total_prefix_tokens=total_tokens,
            breakpoints=tuple(selected),
            skipped=tuple(skipped),
            reason=CACHE_BREAKPOINT_REASON_SELECTED,
        )

    def _select(
        self,
        eligible: Sequence[CacheBreakpointCandidate],
        limit: int,
    ) -> list[CacheBreakpointCandidate]:
        if limit <= 0:
            return []
        frontier = eligible[-1]
        if limit == 1:
            return [frontier]

        selected: list[CacheBreakpointCandidate] = [frontier]
        selected_ids = {frontier.id}

        latest_system = next((
            item for item in reversed(eligible)
            if item.anchor and item.kind == CACHE_BREAKPOINT_KIND_SYSTEM
        ), None)
        tool_anchor = next((
            item for item in eligible
            if item.anchor and item.kind == CACHE_BREAKPOINT_KIND_TOOLS
        ), None)
        for anchor in (latest_system, tool_anchor):
            if len(selected) >= limit:
                break
            if anchor is not None and anchor.id not in selected_ids:
                selected.append(anchor)
                selected_ids.add(anchor.id)

        message_pool = [
            item for item in eligible
            if item.moving
            and item.kind == CACHE_BREAKPOINT_KIND_MESSAGES
            and item.id not in selected_ids
        ]
        remaining = limit - len(selected)
        if remaining > 0 and message_pool:
            for item in self._evenly_spaced(message_pool, remaining):
                selected.append(item)
                selected_ids.add(item.id)

        if len(selected) < limit:
            earlier_systems = [
                item for item in reversed(eligible)
                if item.anchor
                and item.kind == CACHE_BREAKPOINT_KIND_SYSTEM
                and item.id not in selected_ids
            ]
            for item in earlier_systems:
                if len(selected) >= limit:
                    break
                selected.append(item)
                selected_ids.add(item.id)

        return sorted(selected, key=lambda item: (item.order, item.id))

    @staticmethod
    def _evenly_spaced(
        candidates: Sequence[CacheBreakpointCandidate],
        count: int,
    ) -> list[CacheBreakpointCandidate]:
        if count <= 0 or not candidates:
            return []
        if count >= len(candidates):
            return list(candidates)
        if count == 1:
            return [candidates[len(candidates) // 2]]
        indexes = {
            round(index * (len(candidates) - 1) / (count + 1))
            for index in range(1, count + 1)
        }
        # ``round`` can collapse two target positions for tiny histories; fill
        # deterministically from the middle outward if that happens.
        cursor = len(candidates) // 2
        while len(indexes) < count:
            indexes.add(cursor)
            cursor = (cursor + 1) % len(candidates)
        return [candidates[index] for index in sorted(indexes)[:count]]

    def _empty(
        self,
        *,
        mode: str,
        limit: int,
        minimum: int,
        total_tokens: int,
        reason: str,
        candidates: Iterable[CacheBreakpointCandidate] = (),
        skip_reason: str = "",
    ) -> CacheBreakpointPlan:
        skipped = tuple(
            CacheBreakpointSkip(item.id, skip_reason or reason)
            for item in candidates
        )
        return CacheBreakpointPlan(
            schema_version=self.schema_version,
            mode=mode,
            strategy=self.strategy,
            max_breakpoints=limit,
            minimum_cacheable_tokens=minimum,
            total_prefix_tokens=total_tokens,
            skipped=skipped,
            reason=reason,
        )


DEFAULT_CACHE_BREAKPOINT_PLANNER = CacheBreakpointPlanner()


def plan_cache_breakpoints(
    candidates: Sequence[CacheBreakpointCandidate],
    *,
    mode: str,
    max_breakpoints: int,
    minimum_cacheable_tokens: int = 0,
    planner: CacheBreakpointPlanner = DEFAULT_CACHE_BREAKPOINT_PLANNER,
) -> CacheBreakpointPlan:
    """Convenience entry point used by adapters and tests."""
    return planner.plan(
        candidates,
        mode=mode,
        max_breakpoints=max_breakpoints,
        minimum_cacheable_tokens=minimum_cacheable_tokens,
    )


__all__ = [
    "CACHE_BREAKPOINT_KIND_MESSAGES",
    "CACHE_BREAKPOINT_KIND_SYSTEM",
    "CACHE_BREAKPOINT_KIND_TOOLS",
    "CACHE_BREAKPOINT_PLANNER_VERSION",
    "CACHE_BREAKPOINT_REASON_BELOW_MINIMUM",
    "CACHE_BREAKPOINT_REASON_DISABLED",
    "CACHE_BREAKPOINT_REASON_NO_CANDIDATES",
    "CACHE_BREAKPOINT_REASON_SELECTED",
    "CACHE_BREAKPOINT_SKIP_BELOW_MINIMUM",
    "CACHE_BREAKPOINT_SKIP_CAPACITY",
    "CACHE_BREAKPOINT_SKIP_INVALID_ORDER",
    "CACHE_BREAKPOINT_STABILITY_SESSION",
    "CACHE_BREAKPOINT_STABILITY_STATIC",
    "CACHE_BREAKPOINT_STABILITY_TURN",
    "CACHE_BREAKPOINT_STRATEGY",
    "CacheBreakpointCandidate",
    "CacheBreakpointPlan",
    "CacheBreakpointPlanner",
    "CacheBreakpointSkip",
    "DEFAULT_CACHE_BREAKPOINT_PLANNER",
    "plan_cache_breakpoints",
]
