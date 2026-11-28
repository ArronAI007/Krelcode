"""Identity and execution-envelope values shared by contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional


@dataclass(frozen=True)
class RuntimeIdentity:
    """Task-scoped identity and tenant binding."""

    principal_id: str = "local"
    tenant_id: str = "local"
    organization_id: str = ""
    auth_method: str = "local"
    scopes: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "principal_id", "tenant_id", "organization_id", "auth_method"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self,
            "scopes",
            tuple(dict.fromkeys(
                str(item) for item in self.scopes if str(item))),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class HarnessReleaseIdentity:
    """Harness/policy artifacts pinned for one task."""

    release_id: str = ""
    release_hash: str = ""
    prompt_release_id: str = ""
    prompt_release_hash: str = ""
    agent_definition_id: str = ""
    policy_version: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "release_id", "release_hash", "prompt_release_id",
            "prompt_release_hash", "agent_definition_id", "policy_version",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class RuntimeSandboxBinding:
    """Immutable Sandbox/resource envelope selected for one task."""

    profile: str = "workspace"
    backend: str = "auto"
    enabled: bool = True
    network: bool = False
    permission_profile: str = "workspace"
    write_scope: str = "workspace"
    isolation_level: str = "os"
    cpu_seconds: Optional[int] = None
    memory_mb: Optional[int] = None
    storage_mb: Optional[int] = None
    process_limit: Optional[int] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "profile", "backend", "permission_profile", "write_scope",
            "isolation_level",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        for name in (
            "cpu_seconds", "memory_mb", "storage_mb", "process_limit"):
            value = getattr(self, name)
            object.__setattr__(
                self, name, max(1, int(value)) if value is not None else None)
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "network", bool(self.network))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


__all__ = [
    "HarnessReleaseIdentity",
    "RuntimeIdentity",
    "RuntimeSandboxBinding",
]
