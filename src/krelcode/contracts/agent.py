"""Unified Agent Contract for one task.

This is the task-level root object.  It binds identity, intent, inputs,
deliverables, authority, approvals, Sandbox scope, budget, acceptance and
Harness release before the model or any tool can run.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Sequence

from ..core.messages import ImageRef
from .application import (
    AcceptanceContract,
    ApplicationRequest,
    RiskLevel,
    RiskProfile,
    TaskBudget,
    TaskShape,
)
from .agent_authority import (
    ApprovalDecision,
    ApprovalRule,
    AuthorityEnvelope,
)
from .identity import (
    HarnessReleaseIdentity,
    RuntimeIdentity,
    RuntimeSandboxBinding,
)

AGENT_CONTRACT_SCHEMA_VERSION = 1


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, Sequence):
        values = value
    else:
        values = ()
    return tuple(
        text for item in values
        if (text := str(item).strip())
    )


class InputKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    DIRECTORY = "directory"
    URL = "url"
    JSON = "json"


class DeliverableKind(str, Enum):
    RESPONSE = "response"
    FILE = "file"
    DIRECTORY = "directory"
    PATCH = "patch"
    REPORT = "report"
    ARTIFACT = "artifact"
    JSON = "json"


@dataclass(frozen=True)
class InputRequirement:
    id: str
    kind: InputKind | str
    required: bool = True
    media_types: tuple[str, ...] = ()
    schema: Mapping[str, object] = field(default_factory=dict)
    source: str = ""
    max_items: int = 1
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        if not self.id:
            raise ValueError("InputRequirement.id 不能为空")
        object.__setattr__(self, "kind", InputKind(self.kind))
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(
            self,
            "media_types",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.media_types
                if str(item).strip()
            )),
        )
        object.__setattr__(
            self, "schema", MappingProxyType(dict(self.schema)))
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "max_items", max(1, int(self.max_items)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": InputKind(self.kind).value,
            "required": self.required,
            "media_types": list(self.media_types),
            "schema": dict(self.schema),
            "source": self.source,
            "max_items": self.max_items,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "InputRequirement":
        return cls(
            id=str(raw.get("id") or ""),
            kind=str(raw.get("kind") or InputKind.TEXT.value),
            required=bool(raw.get("required", True)),
            media_types=_string_tuple(raw.get("media_types")),
            schema=_mapping(raw.get("schema")),
            source=str(raw.get("source") or ""),
            max_items=_int_value(raw.get("max_items"), 1),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class InputContract:
    requirements: tuple[InputRequirement, ...] = (
        InputRequirement("goal", InputKind.TEXT),
    )
    allow_additional_attachments: bool = True
    max_attachments: int = 8
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requirements", tuple(self.requirements))
        object.__setattr__(
            self,
            "allow_additional_attachments",
            bool(self.allow_additional_attachments),
        )
        object.__setattr__(
            self, "max_attachments", max(0, int(self.max_attachments)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def validate(
        self,
        text: str,
        images: Sequence[ImageRef] = (),
    ) -> tuple[str, ...]:
        errors: list[str] = []
        normalized = str(text or "").strip()
        image_items = tuple(images)
        if len(image_items) > self.max_attachments:
            errors.append(
                f"附件数量 {len(image_items)} 超过上限 {self.max_attachments}")
        for requirement in self.requirements:
            kind = InputKind(requirement.kind)
            if (
                kind is InputKind.TEXT
                and requirement.required
                and not normalized
                and not image_items
            ):
                errors.append(f"缺少必需文本输入: {requirement.id}")
            if kind is InputKind.IMAGE:
                matched = [
                    image for image in image_items
                    if not requirement.media_types
                    or image.media_type in requirement.media_types
                ]
                if requirement.required and not matched:
                    errors.append(f"缺少必需图片输入: {requirement.id}")
                if len(matched) > requirement.max_items:
                    errors.append(
                        f"图片输入 {requirement.id} 数量超过上限 "
                        f"{requirement.max_items}")
        if (
            image_items
            and not self.allow_additional_attachments
            and not any(
                InputKind(item.kind) is InputKind.IMAGE
                for item in self.requirements
            )
        ):
            errors.append("当前输入契约不允许图片附件")
        return tuple(errors)

    def to_dict(self) -> dict[str, object]:
        return {
            "requirements": [
                requirement.to_dict() for requirement in self.requirements
            ],
            "allow_additional_attachments": self.allow_additional_attachments,
            "max_attachments": self.max_attachments,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "InputContract":
        requirements_raw = raw.get("requirements")
        requirements = tuple(
            InputRequirement.from_dict(item)
            for item in (
                requirements_raw
                if isinstance(requirements_raw, list) else ()
            )
            if isinstance(item, Mapping)
        )
        return cls(
            requirements=requirements or (
                InputRequirement("goal", InputKind.TEXT),),
            allow_additional_attachments=bool(
                raw.get("allow_additional_attachments", True)),
            max_attachments=_int_value(raw.get("max_attachments"), 8),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class DeliverableRequirement:
    id: str
    kind: DeliverableKind | str
    required: bool = True
    path: str = ""
    media_type: str = ""
    schema: Mapping[str, object] = field(default_factory=dict)
    artifact_required: bool = False
    max_bytes: Optional[int] = None
    artifact_id: str = ""
    acceptance_evidence: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        if not self.id:
            raise ValueError("DeliverableRequirement.id 不能为空")
        object.__setattr__(self, "kind", DeliverableKind(self.kind))
        object.__setattr__(self, "required", bool(self.required))
        object.__setattr__(self, "path", str(self.path or "").strip())
        object.__setattr__(
            self, "media_type", str(self.media_type or "").strip())
        object.__setattr__(
            self, "schema", MappingProxyType(dict(self.schema)))
        if (
            DeliverableKind(self.kind) is not DeliverableKind.RESPONSE
            and not self.path
            and not self.artifact_required
        ):
            object.__setattr__(self, "artifact_required", True)
        object.__setattr__(
            self, "artifact_required", bool(self.artifact_required))
        if self.max_bytes is not None:
            object.__setattr__(
                self, "max_bytes", max(1, int(self.max_bytes)))
        object.__setattr__(
            self, "artifact_id", str(self.artifact_id or "").strip())
        object.__setattr__(
            self,
            "acceptance_evidence",
            _string_tuple(self.acceptance_evidence),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": DeliverableKind(self.kind).value,
            "required": self.required,
            "path": self.path,
            "media_type": self.media_type,
            "schema": dict(self.schema),
            "artifact_required": self.artifact_required,
            "max_bytes": self.max_bytes,
            "artifact_id": self.artifact_id,
            "acceptance_evidence": list(self.acceptance_evidence),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "DeliverableRequirement":
        return cls(
            id=str(raw.get("id") or ""),
            kind=str(raw.get("kind") or DeliverableKind.RESPONSE.value),
            required=bool(raw.get("required", True)),
            path=str(raw.get("path") or ""),
            media_type=str(raw.get("media_type") or ""),
            schema=_mapping(raw.get("schema")),
            artifact_required=bool(raw.get("artifact_required", False)),
            max_bytes=_optional_int(raw.get("max_bytes")),
            artifact_id=str(raw.get("artifact_id") or ""),
            acceptance_evidence=_string_tuple(
                raw.get("acceptance_evidence")),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class DeliverableContract:
    requirements: tuple[DeliverableRequirement, ...] = (
        DeliverableRequirement("final_response", DeliverableKind.RESPONSE),
    )
    allow_additional: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requirements", tuple(self.requirements))
        object.__setattr__(
            self, "allow_additional", bool(self.allow_additional))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "requirements": [
                requirement.to_dict() for requirement in self.requirements
            ],
            "allow_additional": self.allow_additional,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "DeliverableContract":
        requirements_raw = raw.get("requirements")
        requirements = tuple(
            DeliverableRequirement.from_dict(item)
            for item in (
                requirements_raw
                if isinstance(requirements_raw, list) else ()
            )
            if isinstance(item, Mapping)
        )
        return cls(
            requirements=requirements or (
                DeliverableRequirement(
                    "final_response", DeliverableKind.RESPONSE),),
            allow_additional=bool(raw.get("allow_additional", True)),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class AgentContract:
    intent: ApplicationRequest
    principal: RuntimeIdentity = field(default_factory=RuntimeIdentity)
    harness_release: HarnessReleaseIdentity = field(
        default_factory=HarnessReleaseIdentity)
    sandbox: RuntimeSandboxBinding = field(
        default_factory=RuntimeSandboxBinding)
    inputs: InputContract = field(default_factory=InputContract)
    deliverables: DeliverableContract = field(
        default_factory=DeliverableContract)
    authority: AuthorityEnvelope = field(default_factory=AuthorityEnvelope)
    enforce_acceptance: bool = True
    max_verification_attempts: int = 2
    schema_version: int = AGENT_CONTRACT_SCHEMA_VERSION
    contract_id: str = ""
    contract_hash: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ApplicationRequest):
            raise TypeError("AgentContract.intent 必须是 ApplicationRequest")
        object.__setattr__(self, "schema_version", max(
            1, int(self.schema_version)))
        object.__setattr__(
            self, "enforce_acceptance", bool(self.enforce_acceptance))
        object.__setattr__(
            self,
            "max_verification_attempts",
            max(1, int(self.max_verification_attempts)),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))
        rules = list(self.authority.approval_rules)
        if (
            self.intent.risk.requires_approval
            or self.intent.risk.level in {
                RiskLevel.HIGH,
                RiskLevel.CRITICAL,
            }
        ) and not any(
            ApprovalDecision(rule.decision)
            is ApprovalDecision.REQUIRE_APPROVAL
            for rule in rules
        ):
            rules.append(ApprovalRule(
                id="risk:high",
                decision=ApprovalDecision.REQUIRE_APPROVAL,
                reason=(
                    "任务风险等级要求人工审批: "
                    f"{RiskLevel(self.intent.risk.level).value}"
                ),
            ))
            object.__setattr__(
                self,
                "authority",
                AuthorityEnvelope(
                    allowed_systems=self.authority.allowed_systems,
                    denied_systems=self.authority.denied_systems,
                    allowed_tools=self.authority.allowed_tools,
                    denied_tools=self.authority.denied_tools,
                    allowed_mcp_servers=self.authority.allowed_mcp_servers,
                    denied_mcp_servers=self.authority.denied_mcp_servers,
                    network_allowed=self.authority.network_allowed,
                    allowed_domains=self.authority.allowed_domains,
                    write_scope=self.authority.write_scope,
                    allowed_read_paths=self.authority.allowed_read_paths,
                    allowed_write_paths=self.authority.allowed_write_paths,
                    denied_paths=self.authority.denied_paths,
                    approval_rules=tuple(rules),
                    metadata=self.authority.metadata,
                ),
            )
        computed = self._compute_hash()
        if self.contract_hash and self.contract_hash != computed:
            raise ValueError("AgentContract.contract_hash 不匹配")
        object.__setattr__(self, "contract_hash", computed)
        expected_id = f"agent-{computed[:16]}"
        if self.contract_id and self.contract_id != expected_id:
            raise ValueError("AgentContract.contract_id 不匹配")
        object.__setattr__(self, "contract_id", expected_id)

    @property
    def goal(self) -> str:
        return self.intent.goal

    @property
    def task_shape(self) -> TaskShape:
        return self.intent.task_shape

    @property
    def budget(self) -> TaskBudget:
        return self.intent.budget

    @property
    def acceptance(self) -> AcceptanceContract:
        return self.intent.acceptance

    @property
    def risk(self) -> RiskProfile:
        return self.intent.risk

    def validate_inputs(
        self,
        text: str,
        images: Sequence[ImageRef] = (),
    ) -> tuple[str, ...]:
        return self.inputs.validate(text, images)

    def with_intent(self, intent: ApplicationRequest) -> "AgentContract":
        return AgentContract(
            intent=intent,
            principal=self.principal,
            harness_release=self.harness_release,
            sandbox=self.sandbox,
            inputs=self.inputs,
            deliverables=self.deliverables,
            authority=self.authority,
            enforce_acceptance=self.enforce_acceptance,
            max_verification_attempts=self.max_verification_attempts,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )

    def _compute_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "intent": self.intent.to_dict(),
            "principal": _identity_to_dict(self.principal),
            "harness_release": _release_to_dict(self.harness_release),
            "sandbox": _sandbox_to_dict(self.sandbox),
            "inputs": self.inputs.to_dict(),
            "deliverables": self.deliverables.to_dict(),
            "authority": self.authority.to_dict(),
            "enforce_acceptance": self.enforce_acceptance,
            "max_verification_attempts": self.max_verification_attempts,
            "metadata": _contract_metadata(self.metadata),
        }
        return _digest(payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "contract_hash": self.contract_hash,
            "intent": self.intent.to_dict(),
            "principal": _identity_to_dict(self.principal),
            "harness_release": _release_to_dict(self.harness_release),
            "sandbox": _sandbox_to_dict(self.sandbox),
            "inputs": self.inputs.to_dict(),
            "deliverables": self.deliverables.to_dict(),
            "authority": self.authority.to_dict(),
            "enforce_acceptance": self.enforce_acceptance,
            "max_verification_attempts": self.max_verification_attempts,
            "created_at": self.created_at,
            "metadata": _contract_metadata(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "AgentContract":
        intent_raw = raw.get("intent")
        if not isinstance(intent_raw, Mapping):
            raise ValueError("AgentContract.intent 缺失")
        principal_raw = raw.get("principal")
        release_raw = raw.get("harness_release")
        sandbox_raw = raw.get("sandbox")
        inputs_raw = raw.get("inputs")
        deliverables_raw = raw.get("deliverables")
        authority_raw = raw.get("authority")
        return cls(
            intent=ApplicationRequest.from_mapping(intent_raw),
            principal=_identity_from_dict(principal_raw),
            harness_release=_release_from_dict(release_raw),
            sandbox=_sandbox_from_dict(sandbox_raw),
            inputs=InputContract.from_dict(
                inputs_raw if isinstance(inputs_raw, Mapping) else {}),
            deliverables=DeliverableContract.from_dict(
                deliverables_raw
                if isinstance(deliverables_raw, Mapping) else {}),
            authority=AuthorityEnvelope.from_dict(
                authority_raw if isinstance(authority_raw, Mapping) else {}),
            enforce_acceptance=bool(
                raw.get("enforce_acceptance", True)),
            max_verification_attempts=_int_value(
                raw.get("max_verification_attempts"), 2),
            schema_version=_int_value(
                raw.get("schema_version"), AGENT_CONTRACT_SCHEMA_VERSION),
            contract_id=str(raw.get("contract_id") or ""),
            contract_hash=str(raw.get("contract_hash") or ""),
            created_at=_float_value(raw.get("created_at"), time.time()),
            metadata=_mapping(raw.get("metadata")),
        )


class AgentContractFactory:
    @classmethod
    def from_application(
        cls,
        intent: ApplicationRequest,
        *,
        principal: Optional[RuntimeIdentity] = None,
        harness_release: Optional[HarnessReleaseIdentity] = None,
        sandbox: Optional[RuntimeSandboxBinding] = None,
        tool_allowlist: Optional[Sequence[str]] = None,
        mcp_allowlist: Optional[Sequence[str]] = None,
        inputs: Optional[InputContract] = None,
        deliverables: Optional[DeliverableContract] = None,
        authority: Optional[AuthorityEnvelope] = None,
        metadata: Optional[Mapping[str, object]] = None,
        max_verification_attempts: int = 2,
    ) -> AgentContract:
        resolved_sandbox = sandbox or RuntimeSandboxBinding()
        if authority is None:
            systems = ["workspace"]
            if resolved_sandbox.network:
                systems.append("network")
            if tool_allowlist is None:
                systems.extend(("shell", "mcp"))
            else:
                names = set(tool_allowlist)
                if names.intersection({
                    "run_command", "job_start", "job_output", "job_kill",
                }):
                    systems.append("shell")
                if any(name.startswith("mcp__") for name in names):
                    systems.append("mcp")
            authority = AuthorityEnvelope(
                allowed_systems=tuple(systems),
                allowed_tools=(
                    tuple(tool_allowlist)
                    if tool_allowlist is not None else None),
                allowed_mcp_servers=(
                    tuple(mcp_allowlist)
                    if mcp_allowlist is not None else None),
                network_allowed=resolved_sandbox.network,
                write_scope=resolved_sandbox.write_scope,
            )
        return AgentContract(
            intent=intent,
            principal=principal or RuntimeIdentity(),
            harness_release=harness_release or HarnessReleaseIdentity(),
            sandbox=resolved_sandbox,
            inputs=inputs or InputContract(),
            deliverables=deliverables or DeliverableContract(),
            authority=authority,
            max_verification_attempts=max_verification_attempts,
            metadata=dict(metadata or {}),
        )


def _contract_metadata(
    metadata: Mapping[str, object],
) -> dict[str, object]:
    return {
        str(key): value for key, value in metadata.items()
        if str(key) not in {"secrets", "api_key", "authorization"}
    }


def _identity_to_dict(identity: RuntimeIdentity) -> dict[str, object]:
    return {
        "principal_id": identity.principal_id,
        "tenant_id": identity.tenant_id,
        "organization_id": identity.organization_id,
        "auth_method": identity.auth_method,
        "scopes": list(identity.scopes),
        "metadata": dict(identity.metadata),
    }


def _identity_from_dict(raw: object) -> RuntimeIdentity:
    record = raw if isinstance(raw, Mapping) else {}
    return RuntimeIdentity(
        principal_id=str(record.get("principal_id") or "local"),
        tenant_id=str(record.get("tenant_id") or "local"),
        organization_id=str(record.get("organization_id") or ""),
        auth_method=str(record.get("auth_method") or "local"),
        scopes=_string_tuple(record.get("scopes")),
        metadata=_mapping(record.get("metadata")),
    )


def _release_to_dict(release: HarnessReleaseIdentity) -> dict[str, object]:
    return {
        "release_id": release.release_id,
        "release_hash": release.release_hash,
        "prompt_release_id": release.prompt_release_id,
        "prompt_release_hash": release.prompt_release_hash,
        "agent_definition_id": release.agent_definition_id,
        "policy_version": release.policy_version,
        "metadata": dict(release.metadata),
    }


def _release_from_dict(raw: object) -> HarnessReleaseIdentity:
    record = raw if isinstance(raw, Mapping) else {}
    return HarnessReleaseIdentity(
        release_id=str(record.get("release_id") or ""),
        release_hash=str(record.get("release_hash") or ""),
        prompt_release_id=str(record.get("prompt_release_id") or ""),
        prompt_release_hash=str(record.get("prompt_release_hash") or ""),
        agent_definition_id=str(record.get("agent_definition_id") or ""),
        policy_version=str(record.get("policy_version") or ""),
        metadata=_mapping(record.get("metadata")),
    )


def _sandbox_to_dict(sandbox: RuntimeSandboxBinding) -> dict[str, object]:
    return {
        "profile": sandbox.profile,
        "backend": sandbox.backend,
        "enabled": sandbox.enabled,
        "network": sandbox.network,
        "permission_profile": sandbox.permission_profile,
        "write_scope": sandbox.write_scope,
        "isolation_level": sandbox.isolation_level,
        "cpu_seconds": sandbox.cpu_seconds,
        "memory_mb": sandbox.memory_mb,
        "storage_mb": sandbox.storage_mb,
        "process_limit": sandbox.process_limit,
        "metadata": dict(sandbox.metadata),
    }


def _sandbox_from_dict(raw: object) -> RuntimeSandboxBinding:
    record = raw if isinstance(raw, Mapping) else {}
    return RuntimeSandboxBinding(
        profile=str(record.get("profile") or "workspace"),
        backend=str(record.get("backend") or "auto"),
        enabled=bool(record.get("enabled", True)),
        network=bool(record.get("network", False)),
        permission_profile=str(
            record.get("permission_profile") or "workspace"),
        write_scope=str(record.get("write_scope") or "workspace"),
        isolation_level=str(record.get("isolation_level") or "os"),
        cpu_seconds=_optional_int(record.get("cpu_seconds")),
        memory_mb=_optional_int(record.get("memory_mb")),
        storage_mb=_optional_int(record.get("storage_mb")),
        process_limit=_optional_int(record.get("process_limit")),
        metadata=_mapping(record.get("metadata")),
    )


def _mapping(raw: object) -> dict[str, object]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _int_value(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_value(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AGENT_CONTRACT_SCHEMA_VERSION",
    "AgentContract",
    "AgentContractFactory",
    "ApprovalDecision",
    "ApprovalRule",
    "AuthorityDecision",
    "AuthorityEnvelope",
    "DeliverableContract",
    "DeliverableKind",
    "DeliverableRequirement",
    "InputContract",
    "InputKind",
    "InputRequirement",
]
