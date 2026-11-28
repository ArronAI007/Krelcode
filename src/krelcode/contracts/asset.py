"""Shared governance envelope for reusable context and capability assets."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class AssetScope(str, Enum):
    GLOBAL = "global"
    TENANT = "tenant"
    ORGANIZATION = "organization"
    PROJECT = "project"
    USER = "user"
    SESSION = "session"
    TASK = "task"


class AssetSensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class AssetRetention(str, Enum):
    EPHEMERAL = "ephemeral"
    SESSION = "session"
    TASK = "task"
    PROJECT = "project"
    ORGANIZATION = "organization"
    PERMANENT = "permanent"


class AssetStatus(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    EXPIRED = "expired"
    DELETED = "deleted"


class AssetDeletionMode(str, Enum):
    QUARANTINE = "quarantine"
    CASCADE = "cascade"
    HARD_DELETE = "hard_delete"


@dataclass(frozen=True)
class AssetDeletionPlan:
    root_asset_id: str
    mode: AssetDeletionMode | str = AssetDeletionMode.QUARANTINE
    affected_asset_ids: tuple[str, ...] = ()
    blocked_asset_ids: tuple[str, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "root_asset_id", str(self.root_asset_id or "").strip())
        if not self.root_asset_id:
            raise ValueError("AssetDeletionPlan.root_asset_id 不能为空")
        object.__setattr__(self, "mode", AssetDeletionMode(self.mode))
        object.__setattr__(
            self,
            "affected_asset_ids",
            tuple(dict.fromkeys(self.affected_asset_ids)),
        )
        object.__setattr__(
            self,
            "blocked_asset_ids",
            tuple(dict.fromkeys(self.blocked_asset_ids)),
        )
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_dict(self) -> dict[str, object]:
        return {
            "root_asset_id": self.root_asset_id,
            "mode": AssetDeletionMode(self.mode).value,
            "affected_asset_ids": list(self.affected_asset_ids),
            "blocked_asset_ids": list(self.blocked_asset_ids),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class AssetEnvelope:
    asset_id: str
    asset_type: str
    scope: AssetScope | str = AssetScope.SESSION
    owner: str = ""
    tenant_id: str = ""
    organization_id: str = ""
    project_id: str = ""
    user_id: str = ""
    session_id: str = ""
    task_id: str = ""
    version: str = ""
    sensitivity: AssetSensitivity | str = AssetSensitivity.INTERNAL
    retention: AssetRetention | str = AssetRetention.SESSION
    status: AssetStatus | str = AssetStatus.ACTIVE
    expires_at: float | None = None
    derived_from: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "asset_id", "asset_type", "owner", "tenant_id",
            "organization_id", "project_id", "user_id", "session_id",
            "task_id", "version",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.asset_id or not self.asset_type:
            raise ValueError("AssetEnvelope 缺少 asset_id/asset_type")
        object.__setattr__(self, "scope", AssetScope(self.scope))
        object.__setattr__(
            self, "sensitivity", AssetSensitivity(self.sensitivity))
        object.__setattr__(self, "retention", AssetRetention(self.retention))
        object.__setattr__(self, "status", AssetStatus(self.status))
        object.__setattr__(
            self,
            "expires_at",
            float(self.expires_at) if self.expires_at is not None else None,
        )
        object.__setattr__(
            self,
            "derived_from",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.derived_from
                if str(item).strip()
            )),
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def allows_identity(
        self,
        *,
        principal_id: str = "",
        tenant_id: str = "",
        organization_id: str = "",
        project_id: str = "",
        session_id: str = "",
        task_id: str = "",
    ) -> tuple[bool, str]:
        if AssetStatus(self.status) is not AssetStatus.ACTIVE:
            return False, f"asset-{AssetStatus(self.status).value}"
        checks = (
            ("tenant", self.tenant_id, tenant_id),
            ("organization", self.organization_id, organization_id),
            ("project", self.project_id, project_id),
            ("session", self.session_id, session_id),
            ("task", self.task_id, task_id),
            ("owner", (
                self.user_id
                if AssetScope(self.scope) is AssetScope.USER else ""
            ), principal_id),
        )
        for name, expected, actual in checks:
            if expected and actual and expected != actual:
                return False, f"{name}-mismatch"
            if expected and not actual:
                return False, f"{name}-context-missing"
        return True, ""

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "asset_type": self.asset_type,
            "scope": AssetScope(self.scope).value,
            "owner": self.owner,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "version": self.version,
            "sensitivity": AssetSensitivity(self.sensitivity).value,
            "retention": AssetRetention(self.retention).value,
            "status": AssetStatus(self.status).value,
            "expires_at": self.expires_at,
            "derived_from": list(self.derived_from),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "AssetEnvelope":
        derived = raw.get("derived_from")
        metadata = raw.get("metadata")
        expires = raw.get("expires_at")
        return cls(
            asset_id=str(raw.get("asset_id") or ""),
            asset_type=str(raw.get("asset_type") or ""),
            scope=str(raw.get("scope") or AssetScope.SESSION.value),
            owner=str(raw.get("owner") or ""),
            tenant_id=str(raw.get("tenant_id") or ""),
            organization_id=str(raw.get("organization_id") or ""),
            project_id=str(raw.get("project_id") or ""),
            user_id=str(raw.get("user_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            task_id=str(raw.get("task_id") or ""),
            version=str(raw.get("version") or ""),
            sensitivity=str(
                raw.get("sensitivity") or AssetSensitivity.INTERNAL.value),
            retention=str(
                raw.get("retention") or AssetRetention.SESSION.value),
            status=str(raw.get("status") or AssetStatus.ACTIVE.value),
            expires_at=(
                float(str(expires))
                if expires not in (None, "") else None
            ),
            derived_from=(
                tuple(str(item) for item in derived)
                if isinstance(derived, (list, tuple)) else ()
            ),
            metadata=(
                dict(metadata) if isinstance(metadata, Mapping) else {}
            ),
        )


__all__ = [
    "AssetEnvelope",
    "AssetDeletionMode",
    "AssetDeletionPlan",
    "AssetRetention",
    "AssetScope",
    "AssetSensitivity",
    "AssetStatus",
]
