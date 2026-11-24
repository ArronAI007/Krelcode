"""Content-light prompt omission contracts shared by runtime and snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _text(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _integer(value: object) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


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

    def as_dict(self) -> dict[str, object]:
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


__all__ = ["PromptOmissionSnapshot"]
