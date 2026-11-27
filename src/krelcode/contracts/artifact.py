"""Content-addressed deliverable Artifact Store contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    digest: str
    media_type: str = "application/octet-stream"
    size: int = 0
    name: str = ""
    created_at: float = 0.0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", str(self.artifact_id or "").strip())
        if not self.artifact_id:
            raise ValueError("artifact_id 不能为空")
        object.__setattr__(self, "digest", str(self.digest or "").strip())
        if not self.digest:
            raise ValueError("artifact digest 不能为空")
        object.__setattr__(
            self,
            "media_type",
            str(self.media_type or "application/octet-stream").strip(),
        )
        object.__setattr__(self, "size", max(0, int(self.size)))
        object.__setattr__(self, "name", str(self.name or "").strip())
        object.__setattr__(self, "created_at", max(0.0, float(self.created_at)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "digest": self.digest,
            "media_type": self.media_type,
            "size": self.size,
            "name": self.name,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ArtifactRef":
        metadata = raw.get("metadata")
        return cls(
            artifact_id=str(raw.get("artifact_id") or ""),
            digest=str(raw.get("digest") or ""),
            media_type=str(
                raw.get("media_type") or "application/octet-stream"),
            size=_int_value(raw.get("size"), 0),
            name=str(raw.get("name") or ""),
            created_at=_float_value(raw.get("created_at"), 0.0),
            metadata=(
                dict(metadata) if isinstance(metadata, Mapping) else {}),
        )


def _int_value(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _float_value(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


class ArtifactStorePort(Protocol):
    def put(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        metadata: Optional[Mapping[str, object]] = None,
    ) -> ArtifactRef: ...

    def get(self, artifact_id: str) -> bytes: ...

    def stat(self, artifact_id: str) -> ArtifactRef: ...

    def list(self, *, limit: int = 100) -> Sequence[ArtifactRef]: ...

    def delete(self, artifact_id: str) -> bool: ...


__all__ = ["ArtifactRef", "ArtifactStorePort"]
