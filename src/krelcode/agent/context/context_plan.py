"""Content-light ContextPlan snapshots shared by assembly and session restore.

A context plan describes *which* sources participated in a provider request and
how the resulting context relates to the authoritative history.  It deliberately
stores hashes and source identifiers instead of duplicating prompt text.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence, cast

from krelcode.core.messages import Message
from krelcode.agent.context.context_cache_ledger import ContextCacheLedger
from krelcode.agent.prompt.prompt_plan import PromptPlanSnapshot

if TYPE_CHECKING:
    from krelcode.agent.context.context_source import ContextAssembly


def history_digest(history: Iterable[Message]) -> str:
    """Stable digest for normalized message history (not provider wire data)."""
    payload = []
    for message in history:
        payload.append({
            "role": message.role,
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in (message.tool_calls or ())
            ],
            "tool_call_id": message.tool_call_id or "",
            "tool_name": message.tool_name or "",
            "tool_is_error": message.tool_is_error,
            "tool_meta": message.tool_meta or {},
            "provider_meta": message.provider_meta or {},
            "images": [
                {
                    "media_type": image.media_type,
                    "source_path": image.source_path or "",
                }
                for image in (message.images or ())
            ],
        })
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ContextSourceSnapshot:
    """Per-source Context Manifest entry without content bodies."""

    layer_id: str
    source_type: str
    source_id: str
    scope: str
    version: str
    category: str
    trust: str
    permission_basis: str
    selected_reason: str
    tokens: int
    transform: str
    content_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "layer_id": self.layer_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "scope": self.scope,
            "version": self.version,
            "category": self.category,
            "trust": self.trust,
            "permission_basis": self.permission_basis,
            "selected_reason": self.selected_reason,
            "tokens": self.tokens,
            "transform": self.transform,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ContextSourceSnapshot":
        return cls(
            layer_id=str(raw.get("layer_id") or ""),
            source_type=str(raw.get("source_type") or ""),
            source_id=str(raw.get("source_id") or ""),
            scope=str(raw.get("scope") or ""),
            version=str(raw.get("version") or ""),
            category=str(raw.get("category") or ""),
            trust=str(raw.get("trust") or ""),
            permission_basis=str(raw.get("permission_basis") or ""),
            selected_reason=str(raw.get("selected_reason") or ""),
            tokens=max(0, int(str(raw.get("tokens") or 0))),
            transform=str(raw.get("transform") or ""),
            content_hash=str(raw.get("content_hash") or ""),
        )


@dataclass(frozen=True)
class ContextPlanSnapshot:
    """The source/shape plan that produced or represents one context state."""

    turn_id: str = ""
    step: int = 0
    engine: str = ""
    stable_hash: str = ""
    dynamic_hash: str = ""
    full_hash: str = ""
    layer_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    source_records: tuple[ContextSourceSnapshot, ...] = ()
    omitted_layer_ids: tuple[str, ...] = ()
    budget_decisions: tuple[Mapping[str, object], ...] = ()
    categories: tuple[str, ...] = ()
    trust_levels: tuple[str, ...] = ()
    token_count: int = 0
    history_digest: str = ""
    working_context_sequence: int = 0
    created_at: float = 0.0
    restored_from: str = ""
    prompt_plan: PromptPlanSnapshot = field(default_factory=PromptPlanSnapshot)
    cache_ledger: ContextCacheLedger = field(default_factory=ContextCacheLedger)

    @classmethod
    def from_assembly(
        cls,
        assembly: "ContextAssembly",
        *,
        history: Sequence[Message],
        turn_id: str,
        step: int,
        working_context_sequence: int = 0,
        prompt_plan: PromptPlanSnapshot | None = None,
    ) -> "ContextPlanSnapshot":
        layers = tuple(getattr(assembly, "layers", ()) or ())
        request_plan = prompt_plan or PromptPlanSnapshot()
        return cls(
            turn_id=str(turn_id or ""),
            step=max(0, int(step)),
            engine=str(getattr(assembly, "engine", "") or ""),
            stable_hash=(
                request_plan.stable_hash
                or str(getattr(assembly, "stable_hash", "") or "")
            ),
            dynamic_hash=(
                request_plan.dynamic_hash
                or str(getattr(assembly, "dynamic_hash", "") or "")
            ),
            full_hash=(
                request_plan.full_hash
                or str(getattr(assembly, "full_hash", "") or "")
            ),
            layer_ids=tuple(layer.id for layer in layers),
            sources=tuple(sorted({layer.source for layer in layers})),
            source_records=tuple(
                ContextSourceSnapshot(
                    layer_id=layer.id,
                    source_type=layer.source_type,
                    source_id=layer.source_id,
                    scope=layer.scope,
                    version=layer.version,
                    category=layer.category,
                    trust=layer.trust,
                    permission_basis=layer.permission_basis,
                    selected_reason=layer.selected_reason,
                    tokens=layer.tokens,
                    transform=layer.transform,
                    content_hash=layer.content_hash,
                )
                for layer in layers
            ),
            omitted_layer_ids=tuple(
                getattr(assembly, "omitted_layer_ids", ()) or ()),
            budget_decisions=tuple(
                dict(item) for item in (
                    getattr(assembly, "budget_decisions", ()) or ())
                if isinstance(item, Mapping)
            ),
            categories=tuple(layer.category for layer in layers),
            trust_levels=tuple(sorted({
                str(getattr(layer, "trust", "") or "")
                for layer in layers if str(getattr(layer, "trust", "") or "")
            })),
            token_count=sum(max(0, int(c.tokens)) for c in getattr(
                assembly, "token_contributions", ()) or ()),
            history_digest=(
                request_plan.history_digest or history_digest(history)),
            working_context_sequence=max(0, int(working_context_sequence)),
            created_at=time.time(),
            prompt_plan=request_plan,
        )

    def matches_history(self, history: Sequence[Message]) -> bool:
        return bool(self.history_digest) and self.history_digest == history_digest(history)

    def restored_for(
        self,
        history: Sequence[Message],
        checkpoint_id: str,
        *,
        working_context_sequence: int | None = None,
        invalidations: Sequence[str] = (),
    ) -> "ContextPlanSnapshot":
        ledger = self.cache_ledger
        if ledger.complete_request_hash:
            ledger = ledger.restored(
                checkpoint_id, invalidations=invalidations)
        return replace(
            self,
            history_digest=history_digest(history),
            restored_from=str(checkpoint_id or ""),
            working_context_sequence=(
                max(0, int(working_context_sequence))
                if working_context_sequence is not None
                else self.working_context_sequence
            ),
            cache_ledger=ledger,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "step": self.step,
            "engine": self.engine,
            "stable_hash": self.stable_hash,
            "dynamic_hash": self.dynamic_hash,
            "full_hash": self.full_hash,
            "layer_ids": list(self.layer_ids),
            "sources": list(self.sources),
            "source_records": [
                item.as_dict() for item in self.source_records
            ],
            "omitted_layer_ids": list(self.omitted_layer_ids),
            "budget_decisions": [
                dict(item) for item in self.budget_decisions
            ],
            "categories": list(self.categories),
            "trust_levels": list(self.trust_levels),
            "token_count": self.token_count,
            "history_digest": self.history_digest,
            "working_context_sequence": self.working_context_sequence,
            "created_at": self.created_at,
            "restored_from": self.restored_from,
            "prompt_plan": self.prompt_plan.as_dict(),
            "cache_ledger": self.cache_ledger.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object] | None) -> "ContextPlanSnapshot":
        if not isinstance(raw, Mapping):
            return cls()
        def strings(key: str) -> tuple[str, ...]:
            value = raw.get(key)
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(str(item) for item in value if str(item))
        def numeric_int(key: str) -> int:
            value = raw.get(key, 0)
            if not isinstance(value, (int, float, str, bytes, bytearray)):
                return 0
            try:
                return max(0, int(value))
            except (TypeError, ValueError, OverflowError):
                return 0

        created_value = raw.get("created_at", 0.0)
        try:
            created_at = (
                max(0.0, float(created_value))
                if isinstance(created_value, (int, float, str, bytes))
                else 0.0
            )
        except (TypeError, ValueError, OverflowError):
            created_at = 0.0
        step = numeric_int("step")
        token_count = numeric_int("token_count")
        sequence = numeric_int("working_context_sequence")
        raw_records = raw.get("source_records")
        raw_budget = raw.get("budget_decisions")
        return cls(
            turn_id=str(raw.get("turn_id", "") or ""),
            step=step,
            engine=str(raw.get("engine", "") or ""),
            stable_hash=str(raw.get("stable_hash", "") or ""),
            dynamic_hash=str(raw.get("dynamic_hash", "") or ""),
            full_hash=str(raw.get("full_hash", "") or ""),
            layer_ids=strings("layer_ids"),
            sources=strings("sources"),
            source_records=tuple(
                ContextSourceSnapshot.from_dict(item)
                for item in (
                    raw_records if isinstance(raw_records, (list, tuple)) else ()
                )
                if isinstance(item, Mapping)
            ),
            omitted_layer_ids=strings("omitted_layer_ids"),
            budget_decisions=tuple(
                dict(item)
                for item in (
                    raw_budget if isinstance(raw_budget, (list, tuple)) else ()
                )
                if isinstance(item, Mapping)
            ),
            categories=strings("categories"),
            trust_levels=strings("trust_levels"),
            token_count=token_count,
            history_digest=str(raw.get("history_digest", "") or ""),
            working_context_sequence=sequence,
            created_at=created_at,
            restored_from=str(raw.get("restored_from", "") or ""),
            prompt_plan=PromptPlanSnapshot.from_dict(
                cast(Mapping[str, object], raw["prompt_plan"])
                if isinstance(raw.get("prompt_plan"), Mapping)
                else None),
            cache_ledger=ContextCacheLedger.from_dict(
                cast(Mapping[str, object], raw["cache_ledger"])
                if isinstance(raw.get("cache_ledger"), Mapping)
                else None),
        )


__all__ = [
    "ContextPlanSnapshot",
    "ContextSourceSnapshot",
    "history_digest",
]
