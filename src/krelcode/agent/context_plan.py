"""Content-light ContextPlan snapshots shared by assembly and session restore.

A context plan describes *which* sources participated in a provider request and
how the resulting context relates to the authoritative history.  It deliberately
stores hashes and source identifiers instead of duplicating prompt text.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from ..core.messages import Message


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
class ContextPlanSnapshot:
    """The source/shape plan that produced or represents one context state."""

    turn_id: str = ""
    step: int = 0
    engine: str = ""
    stable_hash: str = ""
    full_hash: str = ""
    layer_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    trust_levels: tuple[str, ...] = ()
    token_count: int = 0
    history_digest: str = ""
    working_context_sequence: int = 0
    created_at: float = 0.0
    restored_from: str = ""

    @classmethod
    def from_assembly(
        cls,
        assembly,
        *,
        history: Sequence[Message],
        turn_id: str,
        step: int,
        working_context_sequence: int = 0,
    ) -> "ContextPlanSnapshot":
        layers = tuple(getattr(assembly, "layers", ()) or ())
        return cls(
            turn_id=str(turn_id or ""),
            step=max(0, int(step)),
            engine=str(getattr(assembly, "engine", "") or ""),
            stable_hash=str(getattr(assembly, "stable_hash", "") or ""),
            full_hash=str(getattr(assembly, "full_hash", "") or ""),
            layer_ids=tuple(layer.id for layer in layers),
            sources=tuple(sorted({layer.source for layer in layers})),
            categories=tuple(layer.category for layer in layers),
            trust_levels=tuple(sorted({
                str(getattr(layer, "trust", "") or "")
                for layer in layers if str(getattr(layer, "trust", "") or "")
            })),
            token_count=sum(max(0, int(c.tokens)) for c in getattr(
                assembly, "token_contributions", ()) or ()),
            history_digest=history_digest(history),
            working_context_sequence=max(0, int(working_context_sequence)),
            created_at=time.time(),
        )

    def matches_history(self, history: Sequence[Message]) -> bool:
        return bool(self.history_digest) and self.history_digest == history_digest(history)

    def restored_for(
        self,
        history: Sequence[Message],
        checkpoint_id: str,
        *,
        working_context_sequence: int | None = None,
    ) -> "ContextPlanSnapshot":
        return replace(
            self,
            history_digest=history_digest(history),
            restored_from=str(checkpoint_id or ""),
            working_context_sequence=(
                max(0, int(working_context_sequence))
                if working_context_sequence is not None
                else self.working_context_sequence
            ),
        )

    def as_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "step": self.step,
            "engine": self.engine,
            "stable_hash": self.stable_hash,
            "full_hash": self.full_hash,
            "layer_ids": list(self.layer_ids),
            "sources": list(self.sources),
            "categories": list(self.categories),
            "trust_levels": list(self.trust_levels),
            "token_count": self.token_count,
            "history_digest": self.history_digest,
            "working_context_sequence": self.working_context_sequence,
            "created_at": self.created_at,
            "restored_from": self.restored_from,
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
        try:
            step = max(0, int(raw.get("step", 0) or 0))
            token_count = max(0, int(raw.get("token_count", 0) or 0))
            sequence = max(0, int(raw.get("working_context_sequence", 0) or 0))
            created_at = float(raw.get("created_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            step = token_count = sequence = 0
            created_at = 0.0
        return cls(
            turn_id=str(raw.get("turn_id", "") or ""),
            step=step,
            engine=str(raw.get("engine", "") or ""),
            stable_hash=str(raw.get("stable_hash", "") or ""),
            full_hash=str(raw.get("full_hash", "") or ""),
            layer_ids=strings("layer_ids"),
            sources=strings("sources"),
            categories=strings("categories"),
            trust_levels=strings("trust_levels"),
            token_count=token_count,
            history_digest=str(raw.get("history_digest", "") or ""),
            working_context_sequence=sequence,
            created_at=created_at,
            restored_from=str(raw.get("restored_from", "") or ""),
        )


__all__ = ["ContextPlanSnapshot", "history_digest"]
