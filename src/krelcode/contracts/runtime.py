"""Provider-neutral runtime and permission values.

These values are shared by the agent loop, tool catalogue, approval policy,
subagents and prompt assembly.  They deliberately depend only on the standard
library so no adapter layer has to import another adapter to describe runtime
state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from types import MappingProxyType
from typing import FrozenSet, Mapping, Optional

RUNTIME_RELEASE_SCHEMA_VERSION = 1

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


class RunMode(str, Enum):
    """当前进程发起查询的运行形态；工具池和权限据此做差异化暴露。"""

    REPL = "repl"
    PRINT = "print"
    JSON = "json"
    SDK = "sdk"
    REVIEW = "review"
    SUBAGENT = "subagent"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    READ_ONLY = "read-only"
    AUTO_APPROVE = "auto-approve"
    PLAN = "plan"


@dataclass(frozen=True)
class RuntimeSnapshot:
    """一轮查询的不可变运行时快照（AppState 的窄化替代）。"""

    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    run_mode: RunMode = RunMode.REPL
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    features: FrozenSet[str] = frozenset()
    disabled_tools: FrozenSet[str] = frozenset()
    alive_mcp_servers: FrozenSet[str] = frozenset()
    mcp_health_known: bool = False
    tool_allowlist: Optional[FrozenSet[str]] = None
    mcp_allowlist: Optional[FrozenSet[str]] = None
    # Organization-level allow/deny patterns are separate from a Skill's
    # temporary allowlist. The tool pool applies them before advertising a
    # schema, so hiding a tool and denying its execution cannot drift apart.
    managed_tool_allowlist: Optional[FrozenSet[str]] = None
    managed_tool_denylist: Optional[FrozenSet[str]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_mode", RunMode(self.run_mode))
        object.__setattr__(
            self, "permission_mode", PermissionMode(self.permission_mode))
        object.__setattr__(self, "features", frozenset(self.features or ()))
        object.__setattr__(
            self, "disabled_tools", frozenset(self.disabled_tools or ()))
        object.__setattr__(
            self, "alive_mcp_servers",
            frozenset(self.alive_mcp_servers or ()))
        if self.tool_allowlist is not None:
            object.__setattr__(
                self, "tool_allowlist", frozenset(self.tool_allowlist))
        if self.mcp_allowlist is not None:
            object.__setattr__(
                self, "mcp_allowlist", frozenset(self.mcp_allowlist))
        if self.managed_tool_allowlist is not None:
            object.__setattr__(
                self, "managed_tool_allowlist",
                frozenset(self.managed_tool_allowlist))
        if self.managed_tool_denylist is not None:
            object.__setattr__(
                self, "managed_tool_denylist",
                frozenset(self.managed_tool_denylist))


@dataclass(frozen=True)
class ArtifactIdentity:
    """Content-addressed artifact/version identity."""

    kind: str
    id: str
    version: str = ""
    digest: str = ""
    required: bool = True
    expected_digest: str = ""
    metadata: Mapping[str, object] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind or "").strip())
        object.__setattr__(self, "id", str(self.id or "").strip())
        object.__setattr__(self, "version", str(self.version or "").strip())
        object.__setattr__(self, "digest", str(self.digest or "").strip())
        object.__setattr__(
            self, "expected_digest",
            str(self.expected_digest or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def artifact_id(self) -> str:
        return self.id


@dataclass(frozen=True)
class RuntimeReleaseManifest:
    """Complete, content-light identity of one runtime composition."""

    agent_definition: Mapping[str, object] = dataclass_field(default_factory=dict)
    prompt_release: Mapping[str, object] = dataclass_field(default_factory=dict)
    model_profile: Mapping[str, object] = dataclass_field(default_factory=dict)
    tool_catalog: Mapping[str, object] = dataclass_field(default_factory=dict)
    mcp_servers: Mapping[str, object] = dataclass_field(default_factory=dict)
    plugins: Mapping[str, object] = dataclass_field(default_factory=dict)
    skills: Mapping[str, object] = dataclass_field(default_factory=dict)
    policy_version: Mapping[str, object] = dataclass_field(default_factory=dict)
    memory_schema: Mapping[str, object] = dataclass_field(default_factory=dict)
    context_engine: Mapping[str, object] = dataclass_field(default_factory=dict)
    code_version: Mapping[str, object] = dataclass_field(default_factory=dict)
    schema_version: int = RUNTIME_RELEASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in _RELEASE_COMPONENTS:
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    dict(value) if isinstance(value, Mapping) else {}),
            )

    @property
    def release_hash(self) -> str:
        return _stable_hash(self.to_dict(include_hashes=False))

    @property
    def release_id(self) -> str:
        return f"runtime-{self.release_hash[:16]}"

    def component_hashes(self) -> dict[str, str]:
        return {
            name: _stable_hash(dict(getattr(self, name)))
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

def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ArtifactIdentity",
    "PermissionMode",
    "RunMode",
    "RUNTIME_RELEASE_SCHEMA_VERSION",
    "RuntimeReleaseManifest",
    "RuntimeSnapshot",
]
