"""Stable, provider-neutral observability records."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

SCHEMA_VERSION = 1
RUNTIME_RELEASE_SCHEMA_VERSION = 1


def utc_timestamp() -> float:
    return time.time()


def stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_RELEASE_COMPONENTS = (
    "agent_definition",
    "prompt_release",
    "model_profile",
    "tool_catalog",
    "mcp_servers",
    "plugins",
    "skills",
    "policy_version",
    "memory_schema",
    "context_engine",
    "code_version",
)


@dataclass(frozen=True)
class RuntimeReleaseManifest:
    """Complete, content-light identity of one runtime composition."""

    agent_definition: Mapping[str, object] = field(default_factory=dict)
    prompt_release: Mapping[str, object] = field(default_factory=dict)
    model_profile: Mapping[str, object] = field(default_factory=dict)
    tool_catalog: Mapping[str, object] = field(default_factory=dict)
    mcp_servers: Mapping[str, object] = field(default_factory=dict)
    plugins: Mapping[str, object] = field(default_factory=dict)
    skills: Mapping[str, object] = field(default_factory=dict)
    policy_version: Mapping[str, object] = field(default_factory=dict)
    memory_schema: Mapping[str, object] = field(default_factory=dict)
    context_engine: Mapping[str, object] = field(default_factory=dict)
    code_version: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = RUNTIME_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in _RELEASE_COMPONENTS:
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                MappingProxyType(dict(value) if isinstance(value, Mapping) else {}),
            )

    @property
    def release_hash(self) -> str:
        return stable_hash(self.to_dict(include_hashes=False))

    @property
    def release_id(self) -> str:
        return f"runtime-{self.release_hash[:16]}"

    def component_hashes(self) -> dict[str, str]:
        return {
            name: stable_hash(dict(getattr(self, name)))
            for name in _RELEASE_COMPONENTS
        }

    def to_dict(self, *, include_hashes: bool = True) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": self.schema_version,
            **{
                name: dict(getattr(self, name))
                for name in _RELEASE_COMPONENTS
            },
        }
        if include_hashes:
            record["release_id"] = self.release_id
            record["release_hash"] = self.release_hash
            record["component_hashes"] = self.component_hashes()
        return record

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "RuntimeReleaseManifest":
        return cls(
            **{
                name: (
                    dict(value)
                    if isinstance((value := raw.get(name)), Mapping) else {}
                )
                for name in _RELEASE_COMPONENTS
            },
            schema_version=int(
                str(raw.get("schema_version")
                    or RUNTIME_RELEASE_SCHEMA_VERSION)),
        )

    def diff(self, other: "RuntimeReleaseManifest") -> dict[str, object]:
        """Return deterministic component-level release differences."""
        left_hashes = self.component_hashes()
        right_hashes = other.component_hashes()
        changed: dict[str, object] = {}
        unchanged: list[str] = []
        for name in _RELEASE_COMPONENTS:
            if left_hashes[name] == right_hashes[name]:
                unchanged.append(name)
                continue
            changed[name] = {
                "left_hash": left_hashes[name],
                "right_hash": right_hashes[name],
                "left": dict(getattr(self, name)),
                "right": dict(getattr(other, name)),
            }
        return {
            "left_release_id": self.release_id,
            "right_release_id": other.release_id,
            "left_release_hash": self.release_hash,
            "right_release_hash": other.release_hash,
            "changed_components": changed,
            "unchanged_components": unchanged,
        }


@dataclass(frozen=True)
class EventEnvelope:
    """One append-only observability event.

    ``sequence`` is contiguous per run except when the bounded sink queue drops
    events.  Consumers use the gap to identify loss instead of pretending the
    trace is complete.  Payloads are always redacted and size-bounded before
    they reach this object.
    """

    run_id: str
    sequence: int
    timestamp: float
    kind: str
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    step: int = 0
    span_id: str = ""
    parent_span_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "step": self.step,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind,
            "name": self.name,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EventEnvelope":
        payload = raw.get("payload") or {}
        return cls(
            run_id=str(raw.get("run_id") or ""),
            sequence=int(raw.get("sequence") or 0),
            timestamp=float(raw.get("timestamp") or 0.0),
            kind=str(raw.get("kind") or "unknown"),
            name=str(raw.get("name") or "unknown"),
            payload=dict(payload) if isinstance(payload, Mapping) else {},
            event_id=str(raw.get("event_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            turn_id=str(raw.get("turn_id") or ""),
            step=int(raw.get("step") or 0),
            span_id=str(raw.get("span_id") or ""),
            parent_span_id=str(raw.get("parent_span_id") or ""),
            schema_version=int(raw.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class RunManifest:
    """Immutable identity and experiment context frozen at run start."""

    run_id: str
    started_at: float
    workspace_root: str
    code_version: str
    provider: str
    model: str
    run_mode: str
    permission_mode: str
    config_hash: str
    tool_catalog_hash: str
    tool_names: tuple[str, ...] = ()
    session_id: str = ""
    experiment_id: str = ""
    experiment_name: str = ""
    experiment_hash: str = ""
    experiment_knobs: Mapping[str, object] = field(default_factory=dict)
    runtime_release: Mapping[str, object] | RuntimeReleaseManifest | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_names", tuple(self.tool_names))
        object.__setattr__(
            self, "experiment_knobs",
            MappingProxyType(dict(self.experiment_knobs)))
        release = self.runtime_release
        if isinstance(release, Mapping):
            release = RuntimeReleaseManifest.from_dict(release)
        object.__setattr__(self, "runtime_release", release)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def manifest_hash(self) -> str:
        return stable_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "workspace_root": self.workspace_root,
            "code_version": self.code_version,
            "provider": self.provider,
            "model": self.model,
            "run_mode": self.run_mode,
            "permission_mode": self.permission_mode,
            "config_hash": self.config_hash,
            "tool_catalog_hash": self.tool_catalog_hash,
            "tool_names": list(self.tool_names),
            "session_id": self.session_id,
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "experiment_hash": self.experiment_hash,
            "experiment_knobs": dict(self.experiment_knobs),
            "runtime_release": (
                self.runtime_release.to_dict()
                if isinstance(self.runtime_release, RuntimeReleaseManifest)
                else None
            ),
            "runtime_release_id": (
                self.runtime_release.release_id
                if isinstance(self.runtime_release, RuntimeReleaseManifest)
                else ""
            ),
            "runtime_release_hash": (
                self.runtime_release.release_hash
                if isinstance(self.runtime_release, RuntimeReleaseManifest)
                else ""
            ),
            "metadata": dict(self.metadata),
        }
        if include_hash:
            record["manifest_hash"] = self.manifest_hash
        return record

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RunManifest":
        names = raw.get("tool_names") or ()
        knobs = raw.get("experiment_knobs") or {}
        metadata = raw.get("metadata") or {}
        release = raw.get("runtime_release")
        return cls(
            run_id=str(raw.get("run_id") or ""),
            started_at=float(raw.get("started_at") or 0.0),
            workspace_root=str(raw.get("workspace_root") or ""),
            code_version=str(raw.get("code_version") or ""),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            run_mode=str(raw.get("run_mode") or ""),
            permission_mode=str(raw.get("permission_mode") or ""),
            config_hash=str(raw.get("config_hash") or ""),
            tool_catalog_hash=str(raw.get("tool_catalog_hash") or ""),
            tool_names=tuple(str(item) for item in names)
            if isinstance(names, (list, tuple)) else (),
            session_id=str(raw.get("session_id") or ""),
            experiment_id=str(raw.get("experiment_id") or ""),
            experiment_name=str(raw.get("experiment_name") or ""),
            experiment_hash=str(raw.get("experiment_hash") or ""),
            experiment_knobs=dict(knobs) if isinstance(knobs, Mapping) else {},
            runtime_release=(
                RuntimeReleaseManifest.from_dict(release)
                if isinstance(release, Mapping) else None
            ),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
            schema_version=int(raw.get("schema_version") or SCHEMA_VERSION),
        )


__all__ = [
    "EventEnvelope",
    "RunManifest",
    "RuntimeReleaseManifest",
    "SCHEMA_VERSION",
    "RUNTIME_RELEASE_SCHEMA_VERSION",
    "stable_hash",
    "utc_timestamp",
]
