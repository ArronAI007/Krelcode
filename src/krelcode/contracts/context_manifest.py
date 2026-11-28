"""Content-light, per-model-call Context Manifest contracts."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ContextPolicyIdentity:
    policy_id: str
    version: str
    digest: str
    configuration: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "policy_id", str(self.policy_id or "").strip())
        object.__setattr__(
            self, "version", str(self.version or "").strip())
        object.__setattr__(self, "digest", str(self.digest or "").strip())
        if not self.policy_id or not self.version:
            raise ValueError("ContextPolicyIdentity 缺少 policy_id/version")
        object.__setattr__(
            self,
            "configuration",
            MappingProxyType(dict(self.configuration)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "digest": self.digest,
            "configuration": dict(self.configuration),
        }


@dataclass(frozen=True)
class ContextSourceManifest:
    source_type: str
    source_id: str
    scope: str = ""
    version: str = ""
    trust_level: str = ""
    permission_basis: str = ""
    selected_reason: str = ""
    token_count: int = 0
    transform: str = "verbatim"
    content_hash: str = ""
    layer_id: str = ""
    category: str = ""

    def __post_init__(self) -> None:
        for name in (
            "source_type", "source_id", "scope", "version", "trust_level",
            "permission_basis", "selected_reason", "transform",
            "content_hash", "layer_id", "category",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self, "token_count", max(0, int(self.token_count)))

    def as_dict(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "scope": self.scope,
            "version": self.version,
            "trust_level": self.trust_level,
            "permission_basis": self.permission_basis,
            "selected_reason": self.selected_reason,
            "token_count": self.token_count,
            "transform": self.transform,
            "content_hash": self.content_hash,
            "layer_id": self.layer_id,
            "category": self.category,
        }


@dataclass(frozen=True)
class ContextOmissionManifest:
    source_id: str
    reason: str
    source_type: str = ""
    scope: str = ""
    token_count: int = 0

    def __post_init__(self) -> None:
        for name in ("source_id", "reason", "source_type", "scope"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self, "token_count", max(0, int(self.token_count)))

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "reason": self.reason,
            "source_type": self.source_type,
            "scope": self.scope,
            "token_count": self.token_count,
        }


@dataclass(frozen=True)
class ContextManifest:
    model_call_id: str
    turn_id: str
    step: int
    context_policy: ContextPolicyIdentity
    sources: tuple[ContextSourceManifest, ...] = ()
    omitted: tuple[ContextOmissionManifest, ...] = ()
    session_id: str = ""
    task_id: str = ""
    history_digest: str = ""
    request_digest: str = ""
    total_tokens: int = 0
    created_at: float = field(default_factory=time.time)
    digest: str = ""

    def __post_init__(self) -> None:
        for name in (
            "model_call_id", "turn_id", "session_id", "task_id",
            "history_digest", "request_digest",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.model_call_id:
            raise ValueError("ContextManifest.model_call_id 不能为空")
        object.__setattr__(self, "step", max(0, int(self.step)))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "omitted", tuple(self.omitted))
        object.__setattr__(
            self, "total_tokens", max(0, int(self.total_tokens)))
        object.__setattr__(self, "created_at", max(0.0, float(self.created_at)))
        expected = _digest(self._payload())
        if self.digest and self.digest != expected:
            raise ValueError("ContextManifest.digest 不匹配")
        object.__setattr__(self, "digest", expected)

    def _payload(self) -> dict[str, object]:
        return {
            "model_call_id": self.model_call_id,
            "turn_id": self.turn_id,
            "step": self.step,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "context_policy": self.context_policy.as_dict(),
            "sources": [item.as_dict() for item in self.sources],
            "omitted": [item.as_dict() for item in self.omitted],
            "history_digest": self.history_digest,
            "request_digest": self.request_digest,
            "total_tokens": self.total_tokens,
            "created_at": self.created_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._payload(), "digest": self.digest}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


__all__ = [
    "ContextManifest",
    "ContextOmissionManifest",
    "ContextPolicyIdentity",
    "ContextSourceManifest",
]
