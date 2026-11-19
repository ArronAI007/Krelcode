"""Unified, content-free ledger for one provider request context state.

The ledger joins the cache-relevant identities that are otherwise split across
PromptPlanSnapshot, ContextUsageSnapshot, attachments, Working Context and
compact checkpoints.  It records hashes and identifiers only, never message or
evidence bodies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence


LEDGER_SCHEMA_VERSION = 1
_RULE_FRAGMENT_IDS = frozenset({
    "managed:rules",
    "user:rules",
    "project:rules",
    "directory:rules",
})


def _text(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(dict.fromkeys(str(item) for item in value if str(item)))


def _hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _attachment_record(
    snapshot: object | None,
) -> tuple[int, tuple[tuple[str, int], ...], str]:
    if snapshot is None:
        return 0, (), ""
    generation = _integer(getattr(snapshot, "generation", 0))
    items = tuple(getattr(snapshot, "items", ()) or ())
    revisions = tuple(sorted(
        (
            _text(getattr(item, "id", "")),
            _integer(getattr(item, "revision", 0)),
            _text(getattr(item, "semantic_key", ""), 128),
        )
        for item in items
        if _text(getattr(item, "id", ""))
    ))
    signature = _hash({
        "generation": generation,
        "attachments": revisions,
    }) if revisions or generation else ""
    return generation, tuple((item[0], item[1]) for item in revisions), signature


def _policy_instruction_hash(fragments: Sequence[object]) -> str:
    records = []
    for fragment in fragments:
        fragment_id = str(getattr(fragment, "id", "") or "")
        if (
            fragment_id in _RULE_FRAGMENT_IDS
            or fragment_id.startswith("agent_definition:")
        ):
            records.append((
                fragment_id,
                str(getattr(fragment, "source", "") or ""),
                str(getattr(fragment, "scope", "") or ""),
                str(getattr(fragment, "content_hash", "") or ""),
            ))
    records.sort()
    return _hash(records) if records else ""


def _instruction_scene_hash(fragments: Sequence[object]) -> str:
    records = []
    for fragment in fragments:
        provenance = getattr(fragment, "provenance", {}) or {}
        role = str(provenance.get("source_role", "") or "").lower() \
            if isinstance(provenance, Mapping) else ""
        source = str(getattr(fragment, "source", "") or "")
        if role == "evidence" or source in {
            "memory", "memory_retrieval", "inherited_memory",
        }:
            continue
        records.append((
            str(getattr(fragment, "id", "") or ""),
            str(getattr(fragment, "source", "") or ""),
            str(getattr(fragment, "scope", "") or ""),
            str(getattr(fragment, "layer", "") or ""),
            str(getattr(fragment, "content_hash", "") or ""),
        ))
    records.sort()
    return _hash(records) if records else ""


def _evidence_scene_hash(fragments: Sequence[object]) -> str:
    records = []
    for fragment in fragments:
        provenance = getattr(fragment, "provenance", {}) or {}
        role = str(provenance.get("source_role", "") or "").lower() \
            if isinstance(provenance, Mapping) else ""
        source = str(getattr(fragment, "source", "") or "")
        if role != "evidence" and source not in {
            "memory", "memory_retrieval", "inherited_memory",
        }:
            continue
        records.append((
            str(getattr(fragment, "id", "") or ""),
            source,
            str(getattr(fragment, "scope", "") or ""),
            str(getattr(fragment, "content_hash", "") or ""),
        ))
    records.sort()
    return _hash(records) if records else ""


def _complete_request_hash(payload: Mapping[str, object]) -> str:
    return _hash(payload)


@dataclass(frozen=True)
class ContextCacheLedgerDiff:
    """Why one request identity differs from the previous concrete request."""

    previous_turn_id: str = ""
    previous_step: int = 0
    previous_complete_request_hash: str = ""
    changed_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    stable_changed: bool = False
    instruction_changed: bool = False
    dynamic_changed: bool = False
    evidence_changed: bool = False
    tool_catalog_changed: bool = False
    attachments_changed: bool = False
    memory_changed: bool = False
    history_changed: bool = False
    working_context_changed: bool = False
    compact_checkpoint_changed: bool = False
    provider_changed: bool = False
    complete_request_changed: bool = False

    @property
    def available(self) -> bool:
        return bool(self.previous_complete_request_hash)

    @property
    def cache_miss(self) -> bool:
        return self.complete_request_changed

    @property
    def prefix_cache_miss(self) -> bool:
        return self.stable_changed or self.provider_changed

    def as_dict(self) -> dict:
        return {
            "previous_turn_id": self.previous_turn_id,
            "previous_step": self.previous_step,
            "previous_complete_request_hash": self.previous_complete_request_hash,
            "changed_fields": list(self.changed_fields),
            "reasons": list(self.reasons),
            "stable_changed": self.stable_changed,
            "instruction_changed": self.instruction_changed,
            "dynamic_changed": self.dynamic_changed,
            "evidence_changed": self.evidence_changed,
            "tool_catalog_changed": self.tool_catalog_changed,
            "attachments_changed": self.attachments_changed,
            "memory_changed": self.memory_changed,
            "history_changed": self.history_changed,
            "working_context_changed": self.working_context_changed,
            "compact_checkpoint_changed": self.compact_checkpoint_changed,
            "provider_changed": self.provider_changed,
            "complete_request_changed": self.complete_request_changed,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> "ContextCacheLedgerDiff":
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            previous_turn_id=_text(raw.get("previous_turn_id")),
            previous_step=_integer(raw.get("previous_step")),
            previous_complete_request_hash=_text(
                raw.get("previous_complete_request_hash"), 128),
            changed_fields=_strings(raw.get("changed_fields")),
            reasons=_strings(raw.get("reasons")),
            stable_changed=bool(raw.get("stable_changed", False)),
            instruction_changed=bool(raw.get("instruction_changed", False)),
            dynamic_changed=bool(raw.get("dynamic_changed", False)),
            evidence_changed=bool(raw.get("evidence_changed", False)),
            tool_catalog_changed=bool(raw.get("tool_catalog_changed", False)),
            attachments_changed=bool(raw.get("attachments_changed", False)),
            memory_changed=bool(raw.get("memory_changed", False)),
            history_changed=bool(raw.get("history_changed", False)),
            working_context_changed=bool(
                raw.get("working_context_changed", False)),
            compact_checkpoint_changed=bool(
                raw.get("compact_checkpoint_changed", False)),
            provider_changed=bool(raw.get("provider_changed", False)),
            complete_request_changed=bool(
                raw.get("complete_request_changed", False)),
        )


@dataclass(frozen=True)
class ContextCacheLedger:
    """One unified cache/context identity for a concrete provider request."""

    schema_version: int = LEDGER_SCHEMA_VERSION
    owner: str = "main"
    turn_id: str = ""
    step: int = 0
    provider: str = ""
    model: str = ""
    stable_hash: str = ""
    dynamic_hash: str = ""
    instruction_hash: str = ""
    evidence_hash: str = ""
    tool_catalog_hash: str = ""
    attachment_generation: int = 0
    attachment_hash: str = ""
    attachment_revisions: tuple[tuple[str, int], ...] = ()
    compact_checkpoint_id: str = ""
    working_context_sequence: int = 0
    memory_evidence_ids: tuple[str, ...] = ()
    history_digest: str = ""
    provider_request_digest: str = ""
    wire_fidelity: str = "estimated"
    complete_request_hash: str = ""
    policy_instruction_hash: str = ""
    parent_policy_instruction_hash: str = ""
    instruction_inheritance: str = "not-applicable"
    restored_from_checkpoint_id: str = ""
    resume_invalidations: tuple[str, ...] = ()
    diff: ContextCacheLedgerDiff = field(default_factory=ContextCacheLedgerDiff)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attachment_revisions", tuple(self.attachment_revisions))
        object.__setattr__(
            self, "memory_evidence_ids", tuple(self.memory_evidence_ids))
        object.__setattr__(
            self, "resume_invalidations", tuple(self.resume_invalidations))
        if not self.complete_request_hash and self.has_request_identity:
            object.__setattr__(
                self, "complete_request_hash",
                _complete_request_hash(self.request_identity()),
            )

    @property
    def has_request_identity(self) -> bool:
        """Whether this record represents an actual provider request."""
        return any((
            self.provider,
            self.model,
            self.stable_hash,
            self.dynamic_hash,
            self.instruction_hash,
            self.evidence_hash,
            self.tool_catalog_hash,
            self.attachment_hash,
            self.compact_checkpoint_id,
            self.working_context_sequence,
            self.memory_evidence_ids,
            self.history_digest,
            self.provider_request_digest,
        ))

    def request_identity(self) -> dict[str, object]:
        """Return the content identities that define one complete request."""
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model": self.model,
            "stable_hash": self.stable_hash,
            "dynamic_hash": self.dynamic_hash,
            "instruction_hash": self.instruction_hash,
            "evidence_hash": self.evidence_hash,
            "tool_catalog_hash": self.tool_catalog_hash,
            "attachment_generation": self.attachment_generation,
            "attachment_hash": self.attachment_hash,
            "attachment_revisions": self.attachment_revisions,
            "compact_checkpoint_id": self.compact_checkpoint_id,
            "working_context_sequence": self.working_context_sequence,
            "memory_evidence_ids": self.memory_evidence_ids,
            "history_digest": self.history_digest,
            "provider_request_digest": self.provider_request_digest,
        }

    @classmethod
    def from_components(
        cls,
        *,
        prompt_plan: object,
        attachment_snapshot: object | None,
        compact_checkpoint_id: str,
        working_context_sequence: int,
        provider: str,
        model: str,
        provider_request_digest: str = "",
        wire_fidelity: str = "estimated",
        owner: str = "main",
        parent_policy_instruction_hash: str = "",
        restored_from_checkpoint_id: str = "",
        resume_invalidations: Sequence[str] = (),
    ) -> "ContextCacheLedger":
        fragments = tuple(getattr(prompt_plan, "fragments", ()) or ())
        generation, revisions, attachment_hash = _attachment_record(
            attachment_snapshot)
        memory_ids = tuple(dict.fromkeys(
            str(getattr(hit, "id", "") or "")
            for hit in (getattr(prompt_plan, "memory_hits", ()) or ())
            if str(getattr(hit, "id", "") or "")
        ))
        policy_hash = _policy_instruction_hash(fragments)
        instruction_hash = (
            str(getattr(prompt_plan, "instruction_hash", "") or "")
            or _instruction_scene_hash(fragments)
        )
        evidence_hash = (
            str(getattr(prompt_plan, "evidence_hash", "") or "")
            or _evidence_scene_hash(fragments)
        )
        parent_policy = str(parent_policy_instruction_hash or "")
        inheritance = (
            "match" if parent_policy and parent_policy == policy_hash
            else "mismatch" if parent_policy else "not-applicable"
        )
        return cls(
            owner=_text(owner, 120) or "main",
            turn_id=str(getattr(prompt_plan, "turn_id", "") or ""),
            step=_integer(getattr(prompt_plan, "step", 0)),
            provider=_text(provider, 120),
            model=_text(model, 200),
            stable_hash=str(getattr(prompt_plan, "stable_hash", "") or ""),
            dynamic_hash=str(getattr(prompt_plan, "dynamic_hash", "") or ""),
            instruction_hash=instruction_hash,
            evidence_hash=evidence_hash,
            tool_catalog_hash=str(
                getattr(prompt_plan, "tool_catalog_hash", "") or ""),
            attachment_generation=generation,
            attachment_hash=attachment_hash,
            attachment_revisions=revisions,
            compact_checkpoint_id=str(compact_checkpoint_id or ""),
            working_context_sequence=_integer(working_context_sequence),
            memory_evidence_ids=memory_ids,
            history_digest=str(
                getattr(prompt_plan, "history_digest", "") or ""),
            provider_request_digest=str(provider_request_digest or ""),
            wire_fidelity=_text(wire_fidelity, 40) or "estimated",
            policy_instruction_hash=policy_hash,
            parent_policy_instruction_hash=parent_policy,
            instruction_inheritance=inheritance,
            restored_from_checkpoint_id=str(
                restored_from_checkpoint_id or ""),
            resume_invalidations=tuple(resume_invalidations),
        )

    def compared_with(
        self, previous: "ContextCacheLedger | None",
    ) -> "ContextCacheLedger":
        if previous is None or not previous.complete_request_hash:
            return self
        return replace(self, diff=_compare_ledgers(previous, self))

    def restored(
        self,
        checkpoint_id: str,
        *,
        invalidations: Sequence[str] = (),
    ) -> "ContextCacheLedger":
        return replace(
            self,
            restored_from_checkpoint_id=str(checkpoint_id or ""),
            resume_invalidations=tuple(dict.fromkeys(
                (*self.resume_invalidations, *invalidations))),
        )

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "owner": self.owner,
            "turn_id": self.turn_id,
            "step": self.step,
            "provider": self.provider,
            "model": self.model,
            "stable_hash": self.stable_hash,
            "dynamic_hash": self.dynamic_hash,
            "instruction_hash": self.instruction_hash,
            "evidence_hash": self.evidence_hash,
            "tool_catalog_hash": self.tool_catalog_hash,
            "attachment_generation": self.attachment_generation,
            "attachment_hash": self.attachment_hash,
            "attachment_revisions": [list(item) for item in self.attachment_revisions],
            "compact_checkpoint_id": self.compact_checkpoint_id,
            "working_context_sequence": self.working_context_sequence,
            "memory_evidence_ids": list(self.memory_evidence_ids),
            "history_digest": self.history_digest,
            "provider_request_digest": self.provider_request_digest,
            "wire_fidelity": self.wire_fidelity,
            "complete_request_hash": self.complete_request_hash,
            "policy_instruction_hash": self.policy_instruction_hash,
            "parent_policy_instruction_hash": self.parent_policy_instruction_hash,
            "instruction_inheritance": self.instruction_inheritance,
            "restored_from_checkpoint_id": self.restored_from_checkpoint_id,
            "resume_invalidations": list(self.resume_invalidations),
            "diff": self.diff.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> "ContextCacheLedger":
        if not isinstance(raw, Mapping):
            return cls()
        revisions_raw = raw.get("attachment_revisions")
        revisions: list[tuple[str, int]] = []
        if isinstance(revisions_raw, (list, tuple)):
            for item in revisions_raw:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                revisions.append((_text(item[0]), _integer(item[1])))
        return cls(
            schema_version=_integer(raw.get("schema_version")) or LEDGER_SCHEMA_VERSION,
            owner=_text(raw.get("owner")) or "main",
            turn_id=_text(raw.get("turn_id")),
            step=_integer(raw.get("step")),
            provider=_text(raw.get("provider"), 120),
            model=_text(raw.get("model"), 200),
            stable_hash=_text(raw.get("stable_hash"), 128),
            dynamic_hash=_text(raw.get("dynamic_hash"), 128),
            instruction_hash=_text(raw.get("instruction_hash"), 128),
            evidence_hash=_text(raw.get("evidence_hash"), 128),
            tool_catalog_hash=_text(raw.get("tool_catalog_hash"), 128),
            attachment_generation=_integer(raw.get("attachment_generation")),
            attachment_hash=_text(raw.get("attachment_hash"), 128),
            attachment_revisions=tuple(revisions),
            compact_checkpoint_id=_text(raw.get("compact_checkpoint_id")),
            working_context_sequence=_integer(raw.get("working_context_sequence")),
            memory_evidence_ids=_strings(raw.get("memory_evidence_ids")),
            history_digest=_text(raw.get("history_digest"), 128),
            provider_request_digest=_text(raw.get("provider_request_digest"), 128),
            wire_fidelity=_text(raw.get("wire_fidelity"), 40) or "estimated",
            complete_request_hash=_text(raw.get("complete_request_hash"), 128),
            policy_instruction_hash=_text(raw.get("policy_instruction_hash"), 128),
            parent_policy_instruction_hash=_text(
                raw.get("parent_policy_instruction_hash"), 128),
            instruction_inheritance=(
                _text(raw.get("instruction_inheritance"), 40)
                or "not-applicable"),
            restored_from_checkpoint_id=_text(
                raw.get("restored_from_checkpoint_id")),
            resume_invalidations=_strings(raw.get("resume_invalidations")),
            diff=ContextCacheLedgerDiff.from_dict(
                raw.get("diff") if isinstance(raw.get("diff"), Mapping) else None),
        )


def _compare_ledgers(
    previous: ContextCacheLedger,
    current: ContextCacheLedger,
) -> ContextCacheLedgerDiff:
    fields = (
        "provider", "model", "stable_hash", "dynamic_hash", "instruction_hash",
        "evidence_hash", "tool_catalog_hash", "attachment_generation",
        "attachment_hash", "attachment_revisions", "compact_checkpoint_id",
        "working_context_sequence", "memory_evidence_ids", "history_digest",
        "provider_request_digest", "complete_request_hash",
    )
    changed = tuple(
        name for name in fields
        if getattr(previous, name) != getattr(current, name)
    )
    stable_changed = previous.stable_hash != current.stable_hash
    instruction_changed = previous.instruction_hash != current.instruction_hash
    dynamic_changed = previous.dynamic_hash != current.dynamic_hash
    evidence_changed = previous.evidence_hash != current.evidence_hash
    tool_changed = previous.tool_catalog_hash != current.tool_catalog_hash
    attachments_changed = (
        previous.attachment_generation != current.attachment_generation
        or previous.attachment_hash != current.attachment_hash
        or previous.attachment_revisions != current.attachment_revisions
    )
    memory_changed = (
        previous.memory_evidence_ids != current.memory_evidence_ids)
    history_changed = previous.history_digest != current.history_digest
    working_changed = (
        previous.working_context_sequence != current.working_context_sequence)
    compact_changed = (
        previous.compact_checkpoint_id != current.compact_checkpoint_id)
    provider_changed = (
        previous.provider != current.provider
        or previous.model != current.model)
    complete_changed = (
        previous.complete_request_hash != current.complete_request_hash)

    reasons: list[str] = []
    if stable_changed:
        reasons.append("system_stable_hash_changed")
    if instruction_changed and not stable_changed:
        reasons.append("instruction_scene_changed")
    if tool_changed:
        reasons.append("tool_catalog_changed")
    if attachments_changed:
        reasons.append("environment_attachments_changed")
    if memory_changed:
        reasons.append("memory_evidence_changed")
    elif evidence_changed and not attachments_changed:
        reasons.append("non_memory_evidence_changed")
    if history_changed:
        reasons.append("history_digest_changed")
    if working_changed:
        reasons.append("working_context_changed")
    if compact_changed:
        reasons.append("compact_checkpoint_changed")
    if provider_changed:
        reasons.append("provider_or_model_changed")
    if dynamic_changed and not any((
        instruction_changed,
        tool_changed,
        attachments_changed,
        evidence_changed,
        history_changed,
        working_changed,
        compact_changed,
    )):
        reasons.append("system_dynamic_hash_changed")
    if complete_changed and not reasons:
        reasons.append("provider_request_digest_changed")
    return ContextCacheLedgerDiff(
        previous_turn_id=previous.turn_id,
        previous_step=previous.step,
        previous_complete_request_hash=previous.complete_request_hash,
        changed_fields=changed,
        reasons=tuple(reasons),
        stable_changed=stable_changed,
        instruction_changed=instruction_changed,
        dynamic_changed=dynamic_changed,
        evidence_changed=evidence_changed,
        tool_catalog_changed=tool_changed,
        attachments_changed=attachments_changed,
        memory_changed=memory_changed,
        history_changed=history_changed,
        working_context_changed=working_changed,
        compact_checkpoint_changed=compact_changed,
        provider_changed=provider_changed,
        complete_request_changed=complete_changed,
    )


__all__ = [
    "ContextCacheLedger",
    "ContextCacheLedgerDiff",
    "LEDGER_SCHEMA_VERSION",
]
