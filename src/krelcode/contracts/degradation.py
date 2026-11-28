"""Structured records for capability, permission or quality degradation."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class DegradationDecision:
    dimension: str
    from_artifact: str
    to_artifact: str
    reason: str
    capability_diff: tuple[str, ...] = ()
    permission_diff: tuple[str, ...] = ()
    quality_risk: str = ""
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "dimension", "from_artifact", "to_artifact", "reason",
            "quality_risk",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.dimension or not self.to_artifact:
            raise ValueError("DegradationDecision 缺少 dimension/to_artifact")
        object.__setattr__(
            self,
            "capability_diff",
            tuple(str(item) for item in self.capability_diff),
        )
        object.__setattr__(
            self,
            "permission_diff",
            tuple(str(item) for item in self.permission_diff),
        )
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "from_artifact": self.from_artifact,
            "to_artifact": self.to_artifact,
            "reason": self.reason,
            "capability_diff": list(self.capability_diff),
            "permission_diff": list(self.permission_diff),
            "quality_risk": self.quality_risk,
            "evidence": dict(self.evidence),
        }


__all__ = ["DegradationDecision"]
