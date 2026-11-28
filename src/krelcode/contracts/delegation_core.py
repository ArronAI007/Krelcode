"""Delegation, handoff, relation and result contracts."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

from .application import TaskBudget
from .delegation_primitives import (
    DelegationAcceptance,
    DelegationCapabilities,
    DelegationKnownContext,
    DelegationOutputContract,
    DelegationRetryPolicy,
    DelegationScope,
    WorkspaceVersion,
    _float,
    _int,
    _mapping,
    _strings,
)


class DelegationKind(str, Enum):
    DELEGATION = "delegation"
    HANDOFF = "handoff"


class DelegationFailurePolicy(str, Enum):
    FAIL_FAST = "fail_fast"
    BEST_EFFORT = "best_effort"
    RETRY_OR_REASSIGN = "retry_or_reassign"
    ESCALATE = "escalate"


class DelegationStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    KILLED = "killed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class DelegationParentContext:
    parent_task_id: str = ""
    parent_turn_id: str = ""
    parent_session_id: str = ""
    parent_contract_id: str = ""
    parent_contract_hash: str = ""
    owner: str = "parent"
    budget: TaskBudget = field(default_factory=TaskBudget)
    workspace_version: WorkspaceVersion = field(
        default_factory=WorkspaceVersion)
    recovery_checkpoint_id: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "parent_task_id", "parent_turn_id", "parent_session_id",
            "parent_contract_id", "parent_contract_hash", "owner",
            "recovery_checkpoint_id",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.owner:
            object.__setattr__(self, "owner", "parent")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_task_id": self.parent_task_id,
            "parent_turn_id": self.parent_turn_id,
            "parent_session_id": self.parent_session_id,
            "parent_contract_id": self.parent_contract_id,
            "parent_contract_hash": self.parent_contract_hash,
            "owner": self.owner,
            "budget": self.budget.to_dict(),
            "workspace_version": self.workspace_version.to_dict(),
            "recovery_checkpoint_id": self.recovery_checkpoint_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "DelegationParentContext":
        value = raw if isinstance(raw, MappingABC) else {}
        budget_raw = value.get("budget")
        workspace_raw = value.get("workspace_version")
        return cls(
            parent_task_id=str(value.get("parent_task_id") or ""),
            parent_turn_id=str(value.get("parent_turn_id") or ""),
            parent_session_id=str(value.get("parent_session_id") or ""),
            parent_contract_id=str(value.get("parent_contract_id") or ""),
            parent_contract_hash=str(value.get("parent_contract_hash") or ""),
            owner=str(value.get("owner") or "parent"),
            budget=(
                TaskBudget.from_mapping(budget_raw)
                if isinstance(budget_raw, MappingABC)
                else TaskBudget()
            ),
            workspace_version=WorkspaceVersion.from_mapping(
                workspace_raw if isinstance(workspace_raw, MappingABC) else None
            ),
            recovery_checkpoint_id=str(
                value.get("recovery_checkpoint_id") or ""),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationContract:
    objective: str
    parent: DelegationParentContext = field(
        default_factory=DelegationParentContext)
    scope: DelegationScope = field(default_factory=DelegationScope)
    known_context: DelegationKnownContext = field(
        default_factory=DelegationKnownContext)
    constraints: tuple[str, ...] = ()
    capabilities: DelegationCapabilities = field(
        default_factory=DelegationCapabilities)
    budget: TaskBudget = field(default_factory=TaskBudget)
    output: DelegationOutputContract = field(
        default_factory=DelegationOutputContract)
    failure_policy: DelegationFailurePolicy | str = (
        DelegationFailurePolicy.BEST_EFFORT)
    retry_policy: DelegationRetryPolicy = field(
        default_factory=DelegationRetryPolicy)
    acceptance: DelegationAcceptance = field(
        default_factory=DelegationAcceptance)
    relation_kind: DelegationKind | str = DelegationKind.DELEGATION
    handoff_state: Mapping[str, object] = field(default_factory=dict)
    recovery_checkpoint_id: str = ""
    task_text: str = ""
    schema_version: int = 1
    delegation_id: str = ""
    contract_hash: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        objective = " ".join(str(self.objective or self.task_text).split())
        if not objective:
            raise ValueError("DelegationContract.objective 不能为空")
        object.__setattr__(self, "objective", objective)
        object.__setattr__(
            self, "failure_policy", _failure_policy(self.failure_policy))
        object.__setattr__(
            self, "relation_kind", _delegation_kind(self.relation_kind))
        object.__setattr__(
            self, "constraints", _strings(self.constraints))
        object.__setattr__(
            self, "handoff_state", MappingProxyType(dict(self.handoff_state)))
        object.__setattr__(
            self, "recovery_checkpoint_id",
            str(self.recovery_checkpoint_id or "").strip())
        object.__setattr__(self, "task_text", str(self.task_text or "").strip())
        object.__setattr__(
            self, "schema_version", max(1, int(self.schema_version)))
        object.__setattr__(self, "created_at", max(0.0, _float(self.created_at)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))
        if (
            DelegationKind(self.relation_kind) is DelegationKind.HANDOFF
            and not self.recovery_checkpoint_id
        ):
            raise ValueError("Handoff 必须提供 recovery_checkpoint_id")
        if (
            DelegationKind(self.relation_kind) is DelegationKind.HANDOFF
            and not self.handoff_state
            and self.known_context.is_empty
        ):
            raise ValueError(
                "Handoff 必须提供 handoff_state 或非空 known_context")
        computed = self._compute_hash()
        if self.contract_hash and self.contract_hash != computed:
            raise ValueError("DelegationContract.contract_hash 不匹配")
        object.__setattr__(self, "contract_hash", computed)
        expected_id = f"delegation-{computed[:16]}"
        if self.delegation_id and self.delegation_id != expected_id:
            raise ValueError("DelegationContract.delegation_id 不匹配")
        object.__setattr__(self, "delegation_id", expected_id)

    def _compute_hash(self) -> str:
        return hashlib.sha256(json.dumps(
            self._hash_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "parent": self.parent.to_dict(),
            "scope": self.scope.to_dict(),
            "known_context": self.known_context.to_dict(),
            "constraints": list(self.constraints),
            "capabilities": self.capabilities.to_dict(),
            "budget": self.budget.to_dict(),
            "output": self.output.to_dict(),
            "failure_policy": DelegationFailurePolicy(
                self.failure_policy).value,
            "retry_policy": self.retry_policy.to_dict(),
            "acceptance": self.acceptance.to_dict(),
            "relation_kind": DelegationKind(self.relation_kind).value,
            "handoff_state": dict(self.handoff_state),
            "recovery_checkpoint_id": self.recovery_checkpoint_id,
            "task_text": self.task_text,
            "metadata": dict(self.metadata),
        }

    @property
    def task(self) -> str:
        return self.task_text or self.objective

    def with_capabilities(
        self,
        capabilities: DelegationCapabilities,
    ) -> "DelegationContract":
        return replace(
            self,
            capabilities=capabilities,
            contract_hash="",
            delegation_id="",
        )

    def with_retry_assignment(
        self,
        definition: str,
    ) -> "DelegationContract":
        capabilities = replace(
            self.capabilities,
            definition=str(definition or self.capabilities.definition),
        )
        return replace(
            self,
            capabilities=capabilities,
            metadata={
                **dict(self.metadata),
                "retry_of": self.delegation_id,
            },
            contract_hash="",
            delegation_id="",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **self._hash_payload(),
            "delegation_id": self.delegation_id,
            "contract_hash": self.contract_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object],
        *,
        parent: Optional[DelegationParentContext] = None,
        task_fallback: str = "",
    ) -> "DelegationContract":
        parent_raw = raw.get("parent")
        return cls(
            objective=str(raw.get("objective") or task_fallback or ""),
            parent=(
                parent if parent is not None else
                DelegationParentContext.from_mapping(
                    parent_raw if isinstance(parent_raw, MappingABC) else None)
            ),
            scope=DelegationScope.from_mapping(_object(raw.get("scope"))),
            known_context=DelegationKnownContext.from_mapping(
                _object(raw.get("known_context"))),
            constraints=_strings(raw.get("constraints")),
            capabilities=DelegationCapabilities.from_mapping(
                _object(raw.get("capabilities"))),
            budget=TaskBudget.from_mapping(_mapping(raw.get("budget"))),
            output=DelegationOutputContract.from_mapping(
                _object(raw.get("output"))),
            failure_policy=_failure_policy(
                raw.get("failure_policy") or "best_effort"),
            retry_policy=DelegationRetryPolicy.from_mapping(
                _object(raw.get("retry_policy"))),
            acceptance=DelegationAcceptance.from_mapping(
                _object(raw.get("acceptance"))),
            relation_kind=_delegation_kind(
                raw.get("relation_kind") or "delegation"),
            handoff_state=_mapping(raw.get("handoff_state")),
            recovery_checkpoint_id=str(
                raw.get("recovery_checkpoint_id") or ""),
            task_text=str(raw.get("task_text") or task_fallback or ""),
            schema_version=_int(raw.get("schema_version"), 1),
            delegation_id=str(raw.get("delegation_id") or ""),
            contract_hash=str(raw.get("contract_hash") or ""),
            created_at=_float(raw.get("created_at"), time.time()),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class HandoffContract:
    handoff_id: str
    delegation_id: str
    from_owner: str
    to_owner: str
    recovery_checkpoint_id: str
    state_digest: str
    state: Mapping[str, object] = field(default_factory=dict)
    accepted: bool = True
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "delegation_id", "from_owner", "to_owner",
            "recovery_checkpoint_id",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.delegation_id or not self.to_owner:
            raise ValueError("HandoffContract 缺少 delegation_id/to_owner")
        if not self.recovery_checkpoint_id:
            raise ValueError("HandoffContract 缺少 recovery_checkpoint_id")
        state = dict(self.state)
        digest = hashlib.sha256(json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
        object.__setattr__(self, "state", MappingProxyType(state))
        object.__setattr__(
            self, "state_digest", str(self.state_digest or digest))
        if self.state_digest != digest:
            raise ValueError("HandoffContract.state_digest 不匹配")
        expected = f"handoff-{digest[:16]}"
        object.__setattr__(
            self, "handoff_id", str(self.handoff_id or expected))
        if self.handoff_id != expected:
            raise ValueError("HandoffContract.handoff_id 不匹配")
        object.__setattr__(self, "accepted", bool(self.accepted))
        object.__setattr__(self, "created_at", max(0.0, _float(self.created_at)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "delegation_id": self.delegation_id,
            "from_owner": self.from_owner,
            "to_owner": self.to_owner,
            "recovery_checkpoint_id": self.recovery_checkpoint_id,
            "state_digest": self.state_digest,
            "state": dict(self.state),
            "accepted": self.accepted,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> Optional["HandoffContract"]:
        value = raw if isinstance(raw, MappingABC) else {}
        if not value:
            return None
        return cls(
            handoff_id=str(value.get("handoff_id") or ""),
            delegation_id=str(value.get("delegation_id") or ""),
            from_owner=str(value.get("from_owner") or ""),
            to_owner=str(value.get("to_owner") or ""),
            recovery_checkpoint_id=str(
                value.get("recovery_checkpoint_id") or ""),
            state_digest=str(value.get("state_digest") or ""),
            state=_mapping(value.get("state")),
            accepted=bool(value.get("accepted", True)),
            created_at=_float(value.get("created_at"), time.time()),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationEvidence:
    id: str
    source: str
    observed_at: float = field(default_factory=time.time)
    workspace_version: WorkspaceVersion = field(
        default_factory=WorkspaceVersion)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        object.__setattr__(self, "source", str(self.source or "subagent").strip())
        if not self.id:
            raise ValueError("DelegationEvidence.id 不能为空")
        object.__setattr__(self, "observed_at", max(0.0, _float(self.observed_at)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "observed_at": self.observed_at,
            "workspace_version": self.workspace_version.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object],
    ) -> "DelegationEvidence":
        workspace = raw.get("workspace_version")
        return cls(
            id=str(raw.get("id") or ""),
            source=str(raw.get("source") or "subagent"),
            observed_at=_float(raw.get("observed_at"), time.time()),
            workspace_version=WorkspaceVersion.from_mapping(
                workspace if isinstance(workspace, MappingABC) else None),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationRelation:
    delegation_id: str
    parent_task_id: str
    parent_turn_id: str
    parent_session_id: str
    agent_id: str
    kind: DelegationKind | str
    from_owner: str
    responsibility_owner: str
    recovery_checkpoint_id: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "delegation_id", "parent_task_id", "parent_turn_id",
            "parent_session_id", "agent_id", "from_owner",
            "responsibility_owner", "recovery_checkpoint_id",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "kind", _delegation_kind(self.kind))
        if not self.delegation_id or not self.agent_id:
            raise ValueError("DelegationRelation 缺少 delegation_id/agent_id")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "delegation_id": self.delegation_id,
            "parent_task_id": self.parent_task_id,
            "parent_turn_id": self.parent_turn_id,
            "parent_session_id": self.parent_session_id,
            "agent_id": self.agent_id,
            "kind": DelegationKind(self.kind).value,
            "from_owner": self.from_owner,
            "responsibility_owner": self.responsibility_owner,
            "recovery_checkpoint_id": self.recovery_checkpoint_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object],
    ) -> "DelegationRelation":
        return cls(
            delegation_id=str(raw.get("delegation_id") or ""),
            parent_task_id=str(raw.get("parent_task_id") or ""),
            parent_turn_id=str(raw.get("parent_turn_id") or ""),
            parent_session_id=str(raw.get("parent_session_id") or ""),
            agent_id=str(raw.get("agent_id") or ""),
            kind=_delegation_kind(raw.get("kind") or "delegation"),
            from_owner=str(raw.get("from_owner") or "parent"),
            responsibility_owner=str(
                raw.get("responsibility_owner") or "parent"),
            recovery_checkpoint_id=str(
                raw.get("recovery_checkpoint_id") or ""),
            metadata=_mapping(raw.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationResult:
    delegation_id: str
    agent_id: str
    status: DelegationStatus | str
    summary: str
    structured_output: Mapping[str, object] = field(default_factory=dict)
    evidence: tuple[DelegationEvidence, ...] = ()
    workspace_version: WorkspaceVersion = field(
        default_factory=WorkspaceVersion)
    produced_at: float = field(default_factory=time.time)
    contract_hash: str = ""
    accepted: bool = False
    validation_errors: tuple[str, ...] = ()
    usage: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", DelegationStatus(self.status))
        object.__setattr__(
            self, "structured_output",
            MappingProxyType(dict(self.structured_output)))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "produced_at", max(0.0, _float(self.produced_at)))
        object.__setattr__(
            self, "validation_errors", _strings(self.validation_errors))
        object.__setattr__(
            self,
            "usage",
            MappingProxyType({
                str(key): max(0.0, _float(value))
                for key, value in self.usage.items()
            }),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "delegation_id": self.delegation_id,
            "agent_id": self.agent_id,
            "status": DelegationStatus(self.status).value,
            "summary": self.summary,
            "structured_output": dict(self.structured_output),
            "evidence": [item.to_dict() for item in self.evidence],
            "workspace_version": self.workspace_version.to_dict(),
            "produced_at": self.produced_at,
            "contract_hash": self.contract_hash,
            "accepted": self.accepted,
            "validation_errors": list(self.validation_errors),
            "usage": dict(self.usage),
            "metadata": dict(self.metadata),
        }

    def as_observation_data(self) -> dict[str, object]:
        return {
            "delegation_id": self.delegation_id,
            "agent_id": self.agent_id,
            "status": DelegationStatus(self.status).value,
            "accepted": self.accepted,
            "validation_errors": list(self.validation_errors),
            "contract_hash": self.contract_hash,
            "workspace_version": self.workspace_version.to_dict(),
            "evidence_ids": [item.id for item in self.evidence],
            "usage": dict(self.usage),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "DelegationResult":
        evidence_raw = raw.get("evidence")
        usage_raw = raw.get("usage")
        workspace = raw.get("workspace_version")
        return cls(
            delegation_id=str(raw.get("delegation_id") or ""),
            agent_id=str(raw.get("agent_id") or ""),
            status=str(raw.get("status") or DelegationStatus.REJECTED.value),
            summary=str(raw.get("summary") or ""),
            structured_output=_mapping(raw.get("structured_output")),
            evidence=tuple(
                DelegationEvidence.from_mapping(item)
                for item in (evidence_raw if isinstance(evidence_raw, list) else ())
                if isinstance(item, MappingABC)
            ),
            workspace_version=WorkspaceVersion.from_mapping(
                workspace if isinstance(workspace, MappingABC) else None),
            produced_at=_float(raw.get("produced_at"), time.time()),
            contract_hash=str(raw.get("contract_hash") or ""),
            accepted=bool(raw.get("accepted", False)),
            validation_errors=_strings(raw.get("validation_errors")),
            usage={
                str(key): _float(value)
                for key, value in (
                    usage_raw.items()
                    if isinstance(usage_raw, MappingABC) else ()
                )
            },
            metadata=_mapping(raw.get("metadata")),
        )


def _failure_policy(value: object) -> DelegationFailurePolicy:
    if isinstance(value, DelegationFailurePolicy):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "failfast": "fail_fast",
        "besteffort": "best_effort",
        "retryorreassign": "retry_or_reassign",
    }
    return DelegationFailurePolicy(aliases.get(
        normalized, normalized or DelegationFailurePolicy.BEST_EFFORT.value))


def _delegation_kind(value: object) -> DelegationKind:
    if isinstance(value, DelegationKind):
        return value
    normalized = str(value or "").strip().lower()
    return DelegationKind(normalized or DelegationKind.DELEGATION.value)


def _object(value: object) -> Optional[Mapping[str, object]]:
    return value if isinstance(value, MappingABC) else None


__all__ = [
    "DelegationContract",
    "DelegationEvidence",
    "DelegationFailurePolicy",
    "DelegationKind",
    "DelegationParentContext",
    "DelegationRelation",
    "DelegationResult",
    "DelegationStatus",
    "HandoffContract",
]
