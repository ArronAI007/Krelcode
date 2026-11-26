"""Central governance contracts shared by distributed enforcement points.

The control plane evaluates one immutable request and returns one immutable
decision. Tool dispatch, Sandbox selection, plugin loading, Prompt release
selection and artifact checks remain in their owning modules, but consume the
same decision instead of re-deriving organization policy independently.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Tuple

from .runtime import ArtifactIdentity, PermissionMode, RuntimeSnapshot
from .service_ports import EnterprisePolicyPort, PrincipalPort


@dataclass(frozen=True)
class PolicyVersion:
    """Version and source identity for the effective policy set."""

    organization: str = ""
    version: str = ""
    source: str = "local"
    digest: str = ""

    @property
    def label(self) -> str:
        return f"{self.organization or 'local'}@{self.version or '0'}"

    @property
    def policy_version(self) -> str:
        return self.version


@dataclass(frozen=True)
class AuditCorrelation:
    """Stable IDs used by distributed audit writers."""

    correlation_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    request_id: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "correlation_id", "session_id", "turn_id", "request_id", "trace_id",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())

    def as_dict(self) -> dict[str, str]:
        return {
            "correlation_id": self.correlation_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
        }

    @property
    def audit_id(self) -> str:
        return self.correlation_id


@dataclass(frozen=True)
class SandboxProfile:
    """Effective Sandbox controls consumed by process-capable tools."""

    name: str = "workspace"
    backend: str = "auto"
    enabled: bool = True
    network: bool = False
    permission_profile: str = "workspace"
    write_scope: str = "workspace"

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name or "workspace").strip())
        object.__setattr__(self, "backend", str(self.backend or "auto").strip())
        object.__setattr__(
            self, "enabled", bool(self.enabled))
        object.__setattr__(self, "network", bool(self.network))
        object.__setattr__(
            self,
            "permission_profile",
            str(self.permission_profile or "workspace").strip(),
        )
        object.__setattr__(
            self, "write_scope", str(self.write_scope or "workspace").strip())


@dataclass(frozen=True)
class PluginTrustContext:
    """Plugin identity and verification state at the governance boundary."""

    name: str = ""
    registry: str = ""
    trust_status: str = ""
    signer: str = ""
    key_id: str = ""
    integrity_digest: str = ""
    required: bool = False
    allowed: bool = True

    def __post_init__(self) -> None:
        for name in (
            "name", "registry", "trust_status", "signer", "key_id",
            "integrity_digest",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "allowed", bool(self.allowed))


@dataclass(frozen=True)
class PromptReleaseIdentity:
    """Immutable Prompt release identity and approval state."""

    release_id: str = ""
    release_hash: str = ""
    profile_hash: str = ""
    expected_release_id: str = ""
    expected_release_hash: str = ""
    approved: bool = True
    required: bool = False

    def __post_init__(self) -> None:
        for name in (
            "release_id", "release_hash", "profile_hash",
            "expected_release_id", "expected_release_hash",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "approved", bool(self.approved))
        object.__setattr__(self, "required", bool(self.required))


class SecurityDecisionValue(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class SecurityDecision:
    """One auditable security decision shared across enforcement points."""

    policy_id: str = "local"
    policy_version: str = "0"
    resource: str = ""
    action: str = ""
    decision: SecurityDecisionValue | str = SecurityDecisionValue.ALLOW
    reason: str = ""
    approval_id: str = ""
    sandbox_profile: SandboxProfile = field(default_factory=SandboxProfile)
    trace_id: str = ""
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "policy_id", "policy_version", "resource", "action", "reason",
            "approval_id", "trace_id",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self, "decision", SecurityDecisionValue(self.decision))
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "resource": self.resource,
            "action": self.action,
            "decision": SecurityDecisionValue(self.decision).value,
            "reason": self.reason,
            "approval_id": self.approval_id,
            "sandbox_profile": {
                "name": self.sandbox_profile.name,
                "backend": self.sandbox_profile.backend,
                "enabled": self.sandbox_profile.enabled,
                "network": self.sandbox_profile.network,
                "permission_profile": self.sandbox_profile.permission_profile,
                "write_scope": self.sandbox_profile.write_scope,
            },
            "trace_id": self.trace_id,
            "evidence": dict(self.evidence),
        }

    @property
    def decision_hash(self) -> str:
        return _stable_hash(self.to_dict())


@dataclass(frozen=True)
class GovernanceRequest:
    """All inputs required for one deterministic governance evaluation."""

    identity: Optional[PrincipalPort] = None
    enterprise_policy: Optional[EnterprisePolicyPort] = None
    policy_version: PolicyVersion = field(default_factory=PolicyVersion)
    runtime: RuntimeSnapshot = field(default_factory=RuntimeSnapshot)
    action: str = ""
    resource: str = ""
    tool_name: str = ""
    tool_arguments: Mapping[str, object] = field(default_factory=dict)
    workspace_root: Path = field(default_factory=Path)
    sandbox_profile: SandboxProfile = field(default_factory=SandboxProfile)
    plugin_trust: PluginTrustContext = field(default_factory=PluginTrustContext)
    prompt_release: PromptReleaseIdentity = field(
        default_factory=PromptReleaseIdentity)
    artifacts: Tuple[ArtifactIdentity, ...] = ()
    audit: AuditCorrelation = field(default_factory=AuditCorrelation)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", str(self.action or "").strip())
        object.__setattr__(self, "resource", str(self.resource or "").strip())
        object.__setattr__(self, "tool_name", str(self.tool_name or "").strip())
        object.__setattr__(
            self, "tool_arguments",
            MappingProxyType(dict(self.tool_arguments)))
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def request_id(self) -> str:
        return self.audit.request_id or self.audit.correlation_id

    @property
    def permission_mode(self) -> PermissionMode:
        return self.runtime.permission_mode

    @property
    def tool_allowlist(self) -> Optional[frozenset[str]]:
        return self.runtime.tool_allowlist


@dataclass(frozen=True)
class GovernanceDecision:
    """One normalized decision consumed by all distributed enforcement points."""

    allowed: bool
    reason: str = ""
    policy_version: PolicyVersion = field(default_factory=PolicyVersion)
    identity_subject: str = ""
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    tool_allowlist: Optional[frozenset[str]] = None
    sandbox_profile: SandboxProfile = field(default_factory=SandboxProfile)
    plugin_trust: PluginTrustContext = field(default_factory=PluginTrustContext)
    prompt_release: PromptReleaseIdentity = field(
        default_factory=PromptReleaseIdentity)
    artifacts: Tuple[ArtifactIdentity, ...] = ()
    audit: AuditCorrelation = field(default_factory=AuditCorrelation)
    constraints: Tuple[str, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)
    security_decision: SecurityDecision = field(
        default_factory=SecurityDecision)
    decided_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", str(self.reason or "").strip())
        object.__setattr__(
            self, "identity_subject",
            str(self.identity_subject or "").strip())
        object.__setattr__(
            self, "permission_mode", PermissionMode(self.permission_mode))
        if self.tool_allowlist is not None:
            object.__setattr__(
                self, "tool_allowlist", frozenset(self.tool_allowlist))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self, "constraints",
            tuple(dict.fromkeys(
                str(value).strip()
                for value in self.constraints
                if str(value).strip()
            )),
        )
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence)))
        if not isinstance(self.security_decision, SecurityDecision) or (
            self.security_decision == SecurityDecision()
        ):
            object.__setattr__(
                self,
                "security_decision",
                SecurityDecision(
                    policy_id=self.policy_version.label,
                    policy_version=self.policy_version.version,
                    reason=self.reason,
                    decision=(
                        SecurityDecisionValue.ALLOW
                        if self.allowed else SecurityDecisionValue.DENY
                    ),
                    sandbox_profile=self.sandbox_profile,
                    trace_id=self.audit.trace_id or self.audit.correlation_id,
                ),
            )

    @property
    def decision_hash(self) -> str:
        payload = {
            "allowed": self.allowed,
            "reason": self.reason,
            "policy": {
                "organization": self.policy_version.organization,
                "version": self.policy_version.version,
                "source": self.policy_version.source,
                "digest": self.policy_version.digest,
            },
            "identity": self.identity_subject,
            "permission_mode": self.permission_mode.value,
            "tool_allowlist": (
                sorted(self.tool_allowlist)
                if self.tool_allowlist is not None else None),
            "sandbox": {
                "name": self.sandbox_profile.name,
                "backend": self.sandbox_profile.backend,
                "enabled": self.sandbox_profile.enabled,
                "network": self.sandbox_profile.network,
                "permission_profile": self.sandbox_profile.permission_profile,
                "write_scope": self.sandbox_profile.write_scope,
            },
            "plugin": {
                "name": self.plugin_trust.name,
                "registry": self.plugin_trust.registry,
                "status": self.plugin_trust.trust_status,
                "signer": self.plugin_trust.signer,
                "key_id": self.plugin_trust.key_id,
                "digest": self.plugin_trust.integrity_digest,
                "required": self.plugin_trust.required,
                "allowed": self.plugin_trust.allowed,
            },
            "prompt": {
                "id": self.prompt_release.release_id,
                "hash": self.prompt_release.release_hash,
                "profile_hash": self.prompt_release.profile_hash,
                "expected_id": self.prompt_release.expected_release_id,
                "expected_hash": self.prompt_release.expected_release_hash,
                "approved": self.prompt_release.approved,
                "required": self.prompt_release.required,
            },
            "artifacts": [
                {
                    "kind": item.kind,
                    "id": item.id,
                    "version": item.version,
                    "digest": item.digest,
                    "expected_digest": item.expected_digest,
                    "required": item.required,
                }
                for item in self.artifacts
            ],
            "audit": self.audit.as_dict(),
            "constraints": list(self.constraints),
            "security_decision_hash": self.security_decision.decision_hash,
        }
        return _stable_hash(payload)

    def artifact(self, artifact_id: str) -> Optional[ArtifactIdentity]:
        normalized = str(artifact_id or "").strip()
        return next(
            (item for item in self.artifacts
            if item.id == normalized),
            None,
        )

    @property
    def effective_permission_mode(self) -> PermissionMode:
        return self.permission_mode

    @property
    def policy(self) -> PolicyVersion:
        return self.policy_version


class ControlPlanePort(Protocol):
    """Single governance evaluator used before distributed enforcement."""

    async def evaluate(self, request: GovernanceRequest) -> GovernanceDecision: ...


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ArtifactIdentity",
    "AuditCorrelation",
    "ControlPlanePort",
    "GovernanceDecision",
    "GovernanceRequest",
    "PluginTrustContext",
    "PolicyVersion",
    "PromptReleaseIdentity",
    "SandboxProfile",
    "SecurityDecision",
    "SecurityDecisionValue",
]
