"""Primitive value objects shared by delegation and handoff contracts."""

from __future__ import annotations

import time
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from .application import AcceptanceContract


@dataclass(frozen=True)
class WorkspaceVersion:
    """Content-light workspace identity used for stale-result detection."""

    git_head: str = ""
    dirty_digest: str = ""
    workspace_digest: str = ""
    captured_at: float = field(default_factory=time.time)
    is_git: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("git_head", "dirty_digest", "workspace_digest"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(
            self, "captured_at", max(0.0, _float(self.captured_at)))
        object.__setattr__(self, "is_git", bool(self.is_git))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def available(self) -> bool:
        return bool(
            self.workspace_digest
            or self.git_head
            or self.dirty_digest
        )

    def matches(self, other: "WorkspaceVersion") -> bool:
        if not self.available or not other.available:
            return False
        if self.workspace_digest and other.workspace_digest:
            return self.workspace_digest == other.workspace_digest
        return (
            self.git_head == other.git_head
            and self.dirty_digest == other.dirty_digest
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "git_head": self.git_head,
            "dirty_digest": self.dirty_digest,
            "workspace_digest": self.workspace_digest,
            "captured_at": self.captured_at,
            "is_git": self.is_git,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "WorkspaceVersion":
        value = raw if isinstance(raw, MappingABC) else {}
        return cls(
            git_head=str(value.get("git_head") or ""),
            dirty_digest=str(value.get("dirty_digest") or ""),
            workspace_digest=str(value.get("workspace_digest") or ""),
            captured_at=_float(value.get("captured_at")),
            is_git=bool(value.get("is_git", False)),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationScope:
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    systems: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("read_paths", "write_paths", "deny_paths", "systems"):
            object.__setattr__(self, name, _strings(getattr(self, name)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "deny_paths": list(self.deny_paths),
            "systems": list(self.systems),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "DelegationScope":
        value = raw if isinstance(raw, MappingABC) else {}
        return cls(
            read_paths=_strings(value.get("read_paths")),
            write_paths=_strings(value.get("write_paths")),
            deny_paths=_strings(value.get("deny_paths")),
            systems=_strings(value.get("systems")),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationKnownContext:
    facts: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    completed_work: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    attempted: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "facts", "assumptions", "decisions", "completed_work",
            "blockers", "attempted",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def is_empty(self) -> bool:
        return not any((
            self.facts, self.assumptions, self.decisions,
            self.completed_work, self.blockers, self.attempted,
        ))

    def to_dict(self) -> dict[str, object]:
        return {
            "facts": list(self.facts),
            "assumptions": list(self.assumptions),
            "decisions": list(self.decisions),
            "completed_work": list(self.completed_work),
            "blockers": list(self.blockers),
            "attempted": list(self.attempted),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "DelegationKnownContext":
        value = raw if isinstance(raw, MappingABC) else {}
        return cls(
            facts=_strings(value.get("facts")),
            assumptions=_strings(value.get("assumptions")),
            decisions=_strings(value.get("decisions")),
            completed_work=_strings(value.get("completed_work")),
            blockers=_strings(value.get("blockers")),
            attempted=_strings(value.get("attempted")),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationCapabilities:
    definition: str = "general-purpose"
    model: str = ""
    allowed_tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    permission_mode: str = ""
    skills: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("definition", "model", "permission_mode"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        for name in ("allowed_tools", "mcp_servers", "skills"):
            object.__setattr__(self, name, _strings(getattr(self, name)))
        if not self.definition:
            object.__setattr__(self, "definition", "general-purpose")
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "definition": self.definition,
            "model": self.model,
            "allowed_tools": list(self.allowed_tools),
            "mcp_servers": list(self.mcp_servers),
            "permission_mode": self.permission_mode,
            "skills": list(self.skills),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "DelegationCapabilities":
        value = raw if isinstance(raw, MappingABC) else {}
        return cls(
            definition=str(value.get("definition") or "general-purpose"),
            model=str(value.get("model") or ""),
            allowed_tools=_strings(value.get("allowed_tools")),
            mcp_servers=_strings(value.get("mcp_servers")),
            permission_mode=str(value.get("permission_mode") or ""),
            skills=_strings(value.get("skills")),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationOutputContract:
    format: str = "text"
    schema: Mapping[str, object] = field(default_factory=dict)
    required_fields: tuple[str, ...] = ()
    evidence_required: bool = True
    max_chars: int = 200_000
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        output_format = str(self.format or "text").strip().lower()
        if output_format not in {"text", "json"}:
            raise ValueError("DelegationOutputContract.format 必须是 text/json")
        object.__setattr__(self, "format", output_format)
        object.__setattr__(
            self, "schema", MappingProxyType(dict(self.schema)))
        object.__setattr__(
            self, "required_fields", _strings(self.required_fields))
        object.__setattr__(
            self, "evidence_required", bool(self.evidence_required))
        object.__setattr__(self, "max_chars", max(1, int(self.max_chars)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "schema": dict(self.schema),
            "required_fields": list(self.required_fields),
            "evidence_required": self.evidence_required,
            "max_chars": self.max_chars,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "DelegationOutputContract":
        value = raw if isinstance(raw, MappingABC) else {}
        return cls(
            format=str(value.get("format") or "text"),
            schema=_mapping(value.get("schema")),
            required_fields=_strings(value.get("required_fields")),
            evidence_required=bool(value.get("evidence_required", True)),
            max_chars=_int(value.get("max_chars"), 200_000),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationRetryPolicy:
    max_retries: int = 0
    allow_reassign: bool = False
    reassign_definition: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_retries", max(0, int(self.max_retries)))
        object.__setattr__(
            self, "allow_reassign", bool(self.allow_reassign))
        object.__setattr__(
            self,
            "reassign_definition",
            str(self.reassign_definition or "").strip(),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "max_retries": self.max_retries,
            "allow_reassign": self.allow_reassign,
            "reassign_definition": self.reassign_definition,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "DelegationRetryPolicy":
        value = raw if isinstance(raw, MappingABC) else {}
        return cls(
            max_retries=_int(value.get("max_retries"), 0),
            allow_reassign=bool(value.get("allow_reassign", False)),
            reassign_definition=str(value.get("reassign_definition") or ""),
            metadata=_mapping(value.get("metadata")),
        )


@dataclass(frozen=True)
class DelegationAcceptance:
    criteria: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    require_workspace_match: bool = True
    max_evidence_age_seconds: float = 1_800.0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "criteria", _strings(self.criteria))
        object.__setattr__(
            self, "required_evidence", _strings(self.required_evidence))
        object.__setattr__(
            self, "require_workspace_match",
            bool(self.require_workspace_match))
        object.__setattr__(
            self, "max_evidence_age_seconds",
            max(1.0, _float(self.max_evidence_age_seconds)),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_acceptance_contract(self) -> AcceptanceContract:
        return AcceptanceContract(
            criteria=self.criteria or (
                "Return a schema-valid, evidence-backed delegated result.",
            ),
            required_evidence=self.required_evidence or (
                "subagent-result",
            ),
            allow_manual_review=True,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "criteria": list(self.criteria),
            "required_evidence": list(self.required_evidence),
            "require_workspace_match": self.require_workspace_match,
            "max_evidence_age_seconds": self.max_evidence_age_seconds,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
    ) -> "DelegationAcceptance":
        value = raw if isinstance(raw, MappingABC) else {}
        return cls(
            criteria=_strings(value.get("criteria")),
            required_evidence=_strings(value.get("required_evidence")),
            require_workspace_match=bool(
                value.get("require_workspace_match", True)),
            max_evidence_age_seconds=_float(
                value.get("max_evidence_age_seconds"), 1_800.0),
            metadata=_mapping(value.get("metadata")),
        )


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, MappingABC) else {}


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, SequenceABC) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        values = value
    else:
        values = ()
    return tuple(dict.fromkeys(
        text for item in values
        if (text := str(item).strip())
    ))


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "DelegationAcceptance",
    "DelegationCapabilities",
    "DelegationKnownContext",
    "DelegationOutputContract",
    "DelegationRetryPolicy",
    "DelegationScope",
    "WorkspaceVersion",
]
