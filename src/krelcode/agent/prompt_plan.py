"""Content-light observability contract for one assembled prompt plan.

The runtime ``PromptPlan`` retains fragment text because it must serialize the
provider request.  This module stores only identifiers, provenance, token
counts and hashes so the same plan can be persisted, compared across requests
and rendered by the command layer without creating another content leak path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Mapping, Optional, Sequence


_MEMORY_LAYER = "L7_MEMORY_EVIDENCE"
_MEMORY_SOURCES = frozenset({
    "memory", "memory_retrieval", "inherited_memory",
})


def _text(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    items = (str(item) for item in value if str(item))
    if isinstance(value, set):
        return tuple(sorted(dict.fromkeys(items)))
    return tuple(dict.fromkeys(items))


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _tool_payload(tool: object) -> dict[str, object]:
    """Normalize one provider-visible tool without retaining its body."""
    if isinstance(tool, Mapping):
        return dict(tool)
    as_dict = getattr(tool, "to_dict", None)
    if callable(as_dict):
        value = as_dict()
        if isinstance(value, Mapping):
            return dict(value)
    payload: dict[str, object] = {}
    for key in ("name", "description", "parameters", "input_schema"):
        if hasattr(tool, key):
            payload[key] = getattr(tool, key)
    return payload


def tool_catalog_hash(tools: Sequence[object]) -> str:
    """Hash provider-visible tool schemas in stable name order."""
    normalized = [_tool_payload(tool) for tool in tools]
    normalized.sort(key=lambda item: str(item.get("name", "")))
    return _hash(normalized)


@dataclass(frozen=True)
class PromptFragmentSnapshot:
    """One fragment's provenance and placement, never its content."""

    id: str
    source: str
    scope: str
    layer: str
    stability: str
    trust: str
    tokens: int
    cacheable: bool
    stable: bool
    content_hash: str
    provenance: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "scope": self.scope,
            "layer": self.layer,
            "stability": self.stability,
            "trust": self.trust,
            "tokens": self.tokens,
            "cacheable": self.cacheable,
            "stable": self.stable,
            "content_hash": self.content_hash,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptFragmentSnapshot":
        return cls(
            id=_text(raw.get("id")),
            source=_text(raw.get("source")),
            scope=_text(raw.get("scope")),
            layer=_text(raw.get("layer")),
            stability=_text(raw.get("stability")),
            trust=_text(raw.get("trust")),
            tokens=_integer(raw.get("tokens")),
            cacheable=bool(raw.get("cacheable", False)),
            stable=bool(raw.get("stable", False)),
            content_hash=_text(raw.get("content_hash"), 128),
            provenance=_safe_provenance(raw.get("provenance")),
        )


@dataclass(frozen=True)
class PromptOmissionSnapshot:
    """A bounded explanation for content excluded from the provider request."""

    id: str
    source: str = ""
    scope: str = ""
    layer: str = ""
    stability: str = ""
    trust: str = ""
    tokens: int = 0
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "scope": self.scope,
            "layer": self.layer,
            "stability": self.stability,
            "trust": self.trust,
            "tokens": self.tokens,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptOmissionSnapshot":
        return cls(
            id=_text(raw.get("id")),
            source=_text(raw.get("source")),
            scope=_text(raw.get("scope")),
            layer=_text(raw.get("layer")),
            stability=_text(raw.get("stability")),
            trust=_text(raw.get("trust")),
            tokens=_integer(raw.get("tokens")),
            reason=_text(raw.get("reason")),
        )


@dataclass(frozen=True)
class PromptMemoryHitSnapshot:
    """One memory entry selected into the current request."""

    id: str
    source: str = "memory"
    scope: str = ""
    kind: str = ""
    origin: str = ""
    tokens: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "scope": self.scope,
            "kind": self.kind,
            "origin": self.origin,
            "tokens": self.tokens,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptMemoryHitSnapshot":
        return cls(
            id=_text(raw.get("id")),
            source=_text(raw.get("source")) or "memory",
            scope=_text(raw.get("scope")),
            kind=_text(raw.get("kind")),
            origin=_text(raw.get("origin")),
            tokens=_integer(raw.get("tokens")),
        )


@dataclass(frozen=True)
class PromptPlanDiff:
    """Content-free comparison with the immediately preceding request."""

    previous_turn_id: str = ""
    previous_step: int = 0
    added_fragments: tuple[str, ...] = ()
    removed_fragments: tuple[str, ...] = ()
    changed_fragments: tuple[str, ...] = ()
    omitted_changed: bool = False
    stable_changed: bool = False
    dynamic_changed: bool = False
    full_changed: bool = False
    history_changed: bool = False
    tool_catalog_changed: bool = False
    memory_changed: bool = False
    engine_changed: bool = False
    stable_token_delta: int = 0
    volatile_token_delta: int = 0
    fragment_token_delta: int = 0

    @property
    def available(self) -> bool:
        return bool(self.previous_turn_id or self.previous_step)

    @property
    def changed(self) -> bool:
        return any((
            self.added_fragments,
            self.removed_fragments,
            self.changed_fragments,
            self.omitted_changed,
            self.stable_changed,
            self.dynamic_changed,
            self.full_changed,
            self.history_changed,
            self.tool_catalog_changed,
            self.memory_changed,
            self.engine_changed,
            self.stable_token_delta,
            self.volatile_token_delta,
            self.fragment_token_delta,
        ))

    def as_dict(self) -> dict:
        return {
            "previous_turn_id": self.previous_turn_id,
            "previous_step": self.previous_step,
            "added_fragments": list(self.added_fragments),
            "removed_fragments": list(self.removed_fragments),
            "changed_fragments": list(self.changed_fragments),
            "omitted_changed": self.omitted_changed,
            "stable_changed": self.stable_changed,
            "dynamic_changed": self.dynamic_changed,
            "full_changed": self.full_changed,
            "history_changed": self.history_changed,
            "tool_catalog_changed": self.tool_catalog_changed,
            "memory_changed": self.memory_changed,
            "engine_changed": self.engine_changed,
            "stable_token_delta": self.stable_token_delta,
            "volatile_token_delta": self.volatile_token_delta,
            "fragment_token_delta": self.fragment_token_delta,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> "PromptPlanDiff":
        if not isinstance(raw, Mapping):
            return cls()

        def signed_integer(key: str) -> int:
            try:
                return int(raw.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            previous_turn_id=_text(raw.get("previous_turn_id")),
            previous_step=_integer(raw.get("previous_step")),
            added_fragments=_strings(raw.get("added_fragments")),
            removed_fragments=_strings(raw.get("removed_fragments")),
            changed_fragments=_strings(raw.get("changed_fragments")),
            omitted_changed=bool(raw.get("omitted_changed", False)),
            stable_changed=bool(raw.get("stable_changed", False)),
            dynamic_changed=bool(raw.get("dynamic_changed", False)),
            full_changed=bool(raw.get("full_changed", False)),
            history_changed=bool(raw.get("history_changed", False)),
            tool_catalog_changed=bool(raw.get("tool_catalog_changed", False)),
            memory_changed=bool(raw.get("memory_changed", False)),
            engine_changed=bool(raw.get("engine_changed", False)),
            stable_token_delta=signed_integer("stable_token_delta"),
            volatile_token_delta=signed_integer("volatile_token_delta"),
            fragment_token_delta=signed_integer("fragment_token_delta"),
        )


@dataclass(frozen=True)
class PromptPlanSnapshot:
    """Persistable request observability without prompt/schema/memory bodies."""

    turn_id: str = ""
    step: int = 0
    engine: str = ""
    fragments: tuple[PromptFragmentSnapshot, ...] = ()
    omitted: tuple[PromptOmissionSnapshot, ...] = ()
    dropped_items: tuple[str, ...] = ()
    stable_tokens: int = 0
    volatile_tokens: int = 0
    fragment_tokens: int = 0
    cache_boundary_chars: int = 0
    stable_hash: str = ""
    dynamic_hash: str = ""
    full_hash: str = ""
    instruction_hash: str = ""
    evidence_hash: str = ""
    history_digest: str = ""
    tool_catalog_hash: str = ""
    tool_count: int = 0
    memory_hits: tuple[PromptMemoryHitSnapshot, ...] = ()
    diff: PromptPlanDiff = field(default_factory=PromptPlanDiff)

    @property
    def stable_fragments(self) -> tuple[PromptFragmentSnapshot, ...]:
        return tuple(item for item in self.fragments if item.stable)

    @property
    def volatile_fragments(self) -> tuple[PromptFragmentSnapshot, ...]:
        return tuple(item for item in self.fragments if not item.stable)

    @classmethod
    def from_prompt_plan(
        cls,
        plan,
        *,
        turn_id: str,
        step: int,
        history_digest: str,
        tools: Sequence[object] = (),
        dropped_items: Sequence[str] = (),
        previous: Optional["PromptPlanSnapshot"] = None,
    ) -> "PromptPlanSnapshot":
        cache = getattr(plan, "cache", None)
        stable_ids = set(getattr(cache, "stable_fragments", ()) or ())
        runtime_fragments = tuple(getattr(plan, "fragments", ()) or ())
        fragments = tuple(
            PromptFragmentSnapshot(
                id=_text(getattr(fragment, "id", "")),
                source=_text(getattr(fragment, "source", "")),
                scope=_text(getattr(fragment, "scope", "")),
                layer=_text(getattr(fragment, "layer", "")),
                stability=_text(getattr(fragment, "stability", "")),
                trust=_text(getattr(fragment, "trust", "")),
                tokens=_integer(getattr(fragment, "tokens", 0)),
                cacheable=bool(getattr(fragment, "cacheable", False)),
                stable=_text(getattr(fragment, "id", "")) in stable_ids,
                content_hash=_content_hash(getattr(fragment, "content", "")),
                provenance=_safe_provenance(
                    getattr(fragment, "metadata", {})),
            )
            for fragment in runtime_fragments
        )
        omitted = _normalize_omissions(
            getattr(plan, "omitted_details", ()) or ())
        budget_omissions = tuple(
            PromptOmissionSnapshot(
                id=_text(item),
                source="workspace",
                scope="project",
                reason="budget",
            )
            for item in dropped_items if _text(item)
        )
        all_omitted = _dedupe_omissions((*omitted, *budget_omissions))
        memory_hits = _memory_hits(runtime_fragments)
        tool_hash = tool_catalog_hash(tools)
        stable_tokens = sum(
            fragment.tokens for fragment in fragments if fragment.stable)
        fragment_tokens = sum(fragment.tokens for fragment in fragments)
        volatile_tokens = max(0, fragment_tokens - stable_tokens)
        stable_hash = str(getattr(cache, "stable_hash", "") or "")
        dynamic_hash = str(getattr(cache, "dynamic_hash", "") or "")
        full_hash = _prompt_full_hash(
            stable_hash=stable_hash,
            dynamic_hash=dynamic_hash,
            history_digest=str(history_digest or ""),
            tool_catalog_hash=tool_hash,
            fragments=fragments,
            omitted=all_omitted,
        )
        snapshot = cls(
            turn_id=str(turn_id or ""),
            step=max(0, int(step)),
            engine=str(getattr(plan, "engine", "") or ""),
            fragments=fragments,
            omitted=all_omitted,
            dropped_items=tuple(dict.fromkeys(
                _text(item) for item in dropped_items if _text(item))),
            stable_tokens=stable_tokens,
            volatile_tokens=volatile_tokens,
            fragment_tokens=fragment_tokens,
            cache_boundary_chars=_integer(getattr(cache, "offset", 0)),
            stable_hash=stable_hash,
            dynamic_hash=dynamic_hash,
            full_hash=full_hash,
            instruction_hash=str(
                getattr(plan, "instruction_hash", "") or ""),
            evidence_hash=str(getattr(plan, "evidence_hash", "") or ""),
            history_digest=str(history_digest or ""),
            tool_catalog_hash=tool_hash,
            tool_count=len(tools),
            memory_hits=memory_hits,
        )
        return _with_diff(snapshot, previous)

    def as_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "step": self.step,
            "engine": self.engine,
            "fragments": [item.as_dict() for item in self.fragments],
            "omitted": [item.as_dict() for item in self.omitted],
            "dropped_items": list(self.dropped_items),
            "stable_tokens": self.stable_tokens,
            "volatile_tokens": self.volatile_tokens,
            "fragment_tokens": self.fragment_tokens,
            "cache_boundary_chars": self.cache_boundary_chars,
            "stable_hash": self.stable_hash,
            "dynamic_hash": self.dynamic_hash,
            "full_hash": self.full_hash,
            "instruction_hash": self.instruction_hash,
            "evidence_hash": self.evidence_hash,
            "history_digest": self.history_digest,
            "tool_catalog_hash": self.tool_catalog_hash,
            "tool_count": self.tool_count,
            "memory_hits": [item.as_dict() for item in self.memory_hits],
            "diff": self.diff.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> "PromptPlanSnapshot":
        if not isinstance(raw, Mapping):
            return cls()

        def mappings(key: str) -> tuple[Mapping[str, object], ...]:
            value = raw.get(key)
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(item for item in value if isinstance(item, Mapping))

        return cls(
            turn_id=_text(raw.get("turn_id")),
            step=_integer(raw.get("step")),
            engine=_text(raw.get("engine")),
            fragments=tuple(
                PromptFragmentSnapshot.from_dict(item)
                for item in mappings("fragments")
            ),
            omitted=tuple(
                PromptOmissionSnapshot.from_dict(item)
                for item in mappings("omitted")
            ),
            dropped_items=_strings(raw.get("dropped_items")),
            stable_tokens=_integer(raw.get("stable_tokens")),
            volatile_tokens=_integer(raw.get("volatile_tokens")),
            fragment_tokens=_integer(raw.get("fragment_tokens")),
            cache_boundary_chars=_integer(raw.get("cache_boundary_chars")),
            stable_hash=_text(raw.get("stable_hash"), 128),
            dynamic_hash=_text(raw.get("dynamic_hash"), 128),
            full_hash=_text(raw.get("full_hash"), 128),
            instruction_hash=_text(raw.get("instruction_hash"), 128),
            evidence_hash=_text(raw.get("evidence_hash"), 128),
            history_digest=_text(raw.get("history_digest"), 128),
            tool_catalog_hash=_text(raw.get("tool_catalog_hash"), 128),
            tool_count=_integer(raw.get("tool_count")),
            memory_hits=tuple(
                PromptMemoryHitSnapshot.from_dict(item)
                for item in mappings("memory_hits")
            ),
            diff=PromptPlanDiff.from_dict(
                raw.get("diff") if isinstance(raw.get("diff"), Mapping) else None),
        )


def _normalize_omissions(
    values: Sequence[object],
) -> tuple[PromptOmissionSnapshot, ...]:
    normalized: list[PromptOmissionSnapshot] = []
    for value in values:
        if isinstance(value, PromptOmissionSnapshot):
            normalized.append(value)
        elif isinstance(value, Mapping):
            normalized.append(PromptOmissionSnapshot.from_dict(value))
        elif value is not None and str(value):
            normalized.append(PromptOmissionSnapshot(
                id=str(value), reason="unspecified"))
    return tuple(normalized)


def _dedupe_omissions(
    values: Sequence[PromptOmissionSnapshot],
) -> tuple[PromptOmissionSnapshot, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[PromptOmissionSnapshot] = []
    for value in values:
        key = (value.id, value.reason)
        if not value.id or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _memory_hits(
    fragments: Sequence[object],
) -> tuple[PromptMemoryHitSnapshot, ...]:
    hits: dict[str, PromptMemoryHitSnapshot] = {}
    for fragment in fragments:
        layer = str(getattr(fragment, "layer", "") or "")
        source = str(getattr(fragment, "source", "") or "")
        metadata = getattr(fragment, "metadata", {}) or {}
        if layer != _MEMORY_LAYER and source not in _MEMORY_SOURCES:
            continue

        raw_hits = metadata.get("memory_hits", ())
        accepted_hits = False
        if isinstance(raw_hits, (list, tuple)):
            for raw in raw_hits:
                if not isinstance(raw, Mapping):
                    continue
                hit_id = _text(raw.get("id"))
                if not hit_id:
                    continue
                hits[hit_id] = PromptMemoryHitSnapshot(
                    id=hit_id,
                    source=_text(raw.get("source")) or source or "memory",
                    scope=_text(raw.get("scope")),
                    kind=_text(raw.get("kind")),
                    origin=_text(raw.get("origin")),
                    tokens=_integer(raw.get("tokens")),
                )
                accepted_hits = True
            if accepted_hits:
                continue

        raw_ids = metadata.get("memory_entry_ids", ())
        if not isinstance(raw_ids, (list, tuple, set)):
            raw_ids = metadata.get("evidence_ids", ())
        ids: tuple[str, ...] = ()
        if isinstance(raw_ids, (list, tuple, set)):
            ids = tuple(dict.fromkeys(
                _text(item) for item in raw_ids if _text(item)))
        scopes = metadata.get("memory_scope_counts", {})
        scope = ""
        if isinstance(scopes, Mapping) and len(scopes) == 1:
            scope = _text(next(iter(scopes)))
        aggregate_tokens = _integer(metadata.get("memory_tokens"))
        per_hit_tokens = aggregate_tokens // len(ids) if ids else aggregate_tokens
        origin = _text(
            metadata.get("memory_origin")
            or metadata.get("evidence_origin")
            or metadata.get("origin"))
        for hit_id in ids:
            hits.setdefault(hit_id, PromptMemoryHitSnapshot(
                id=hit_id,
                source=source or "memory",
                scope=scope,
                kind=_text(metadata.get("memory_kind")) or "memory",
                origin=origin,
                tokens=per_hit_tokens,
            ))
    return tuple(hits.values())


_PROVENANCE_KEYS = frozenset({
    "source_role",
    "instruction_source",
    "instruction",
    "requires_validation",
    "evidence_type",
    "prompt_section",
    "prompt_section_required",
    "prompt_profile",
    "prompt_profile_requested",
    "prompt_profile_match",
    "prompt_provider",
    "prompt_model",
    "base_prompt_hash",
    "rule_scope",
    "rule_precedence",
    "rule_overrides",
    "rule_conflict_policy",
    "rule_sources",
    "rule_entry_sources",
    "rule_import_count",
    "rule_issues",
    "rule_targets",
    "truncated",
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


def _safe_provenance(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: _provenance_value(raw.get(key))
        for key in sorted(_PROVENANCE_KEYS)
        if key in raw
    }


def _provenance_value(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, str):
        return _text(value, 500)
    if isinstance(value, (list, tuple)):
        return tuple(
            _provenance_value(item)
            for item in value[:20]
        )
    if isinstance(value, Mapping):
        return {
            _text(key, 80): _provenance_value(item)
            for key, item in list(value.items())[:20]
        }
    return _text(value, 120)


def _prompt_full_hash(
    *,
    stable_hash: str,
    dynamic_hash: str,
    history_digest: str,
    tool_catalog_hash: str,
    fragments: Sequence[PromptFragmentSnapshot],
    omitted: Sequence[PromptOmissionSnapshot],
) -> str:
    """Hash the complete provider-neutral request shape and content identities."""
    return _hash({
        "stable_hash": stable_hash,
        "dynamic_hash": dynamic_hash,
        "history_digest": history_digest,
        "tool_catalog_hash": tool_catalog_hash,
        "fragments": [
            [
                item.id,
                item.source,
                item.scope,
                item.layer,
                item.stability,
                item.trust,
                item.tokens,
                item.stable,
                item.content_hash,
            ]
            for item in fragments
        ],
        "omitted": [
            [item.id, item.source, item.scope, item.reason, item.tokens]
            for item in omitted
        ],
    })


def _with_diff(
    current: PromptPlanSnapshot,
    previous: Optional[PromptPlanSnapshot],
) -> PromptPlanSnapshot:
    if previous is None or not previous.turn_id:
        return current
    current_map = {item.id: item for item in current.fragments}
    previous_map = {item.id: item for item in previous.fragments}
    added = tuple(sorted(set(current_map) - set(previous_map)))
    removed = tuple(sorted(set(previous_map) - set(current_map)))
    changed = tuple(sorted(
        key for key in set(current_map) & set(previous_map)
        if (
            current_map[key].content_hash != previous_map[key].content_hash
            or current_map[key].tokens != previous_map[key].tokens
            or current_map[key].scope != previous_map[key].scope
            or current_map[key].stability != previous_map[key].stability
            or current_map[key].cacheable != previous_map[key].cacheable
            or current_map[key].stable != previous_map[key].stable
        )
    ))
    current_omitted = tuple(
        (item.id, item.source, item.reason) for item in current.omitted)
    previous_omitted = tuple(
        (item.id, item.source, item.reason) for item in previous.omitted)
    current_memory = tuple(
        (item.id, item.source, item.scope, item.kind, item.origin)
        for item in current.memory_hits)
    previous_memory = tuple(
        (item.id, item.source, item.scope, item.kind, item.origin)
        for item in previous.memory_hits)
    return replace(current, diff=PromptPlanDiff(
        previous_turn_id=previous.turn_id,
        previous_step=previous.step,
        added_fragments=added,
        removed_fragments=removed,
        changed_fragments=changed,
        omitted_changed=current_omitted != previous_omitted,
        stable_changed=current.stable_hash != previous.stable_hash,
        dynamic_changed=current.dynamic_hash != previous.dynamic_hash,
        full_changed=current.full_hash != previous.full_hash,
        history_changed=(
            bool(previous.history_digest)
            and previous.history_digest != current.history_digest),
        tool_catalog_changed=(
            current.tool_catalog_hash != previous.tool_catalog_hash),
        memory_changed=current_memory != previous_memory,
        engine_changed=current.engine != previous.engine,
        stable_token_delta=current.stable_tokens - previous.stable_tokens,
        volatile_token_delta=current.volatile_tokens - previous.volatile_tokens,
        fragment_token_delta=current.fragment_tokens - previous.fragment_tokens,
    ))


__all__ = [
    "PromptFragmentSnapshot",
    "PromptMemoryHitSnapshot",
    "PromptOmissionSnapshot",
    "PromptPlanDiff",
    "PromptPlanSnapshot",
    "tool_catalog_hash",
]
