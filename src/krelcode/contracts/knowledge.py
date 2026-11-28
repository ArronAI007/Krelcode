"""Knowledge-source, indexing and retrieval contracts.

Knowledge is organization content, not model-generated memory. It remains
evidence-only and must carry source, version, freshness and access metadata.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Sequence

from .asset import AssetEnvelope, AssetRetention, AssetScope


class KnowledgeScope(str, Enum):
    USER = "user"
    PROJECT = "project"
    ORGANIZATION = "organization"
    SESSION = "session"


class KnowledgeFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class KnowledgeSourceKind(str, Enum):
    LOCAL_DOCUMENTS = "local_documents"
    MCP = "mcp"
    WEB = "web"
    CUSTOM = "custom"


@dataclass(frozen=True)
class KnowledgeVersion:
    version: str = ""
    digest: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class KnowledgeAccessPolicy:
    """Pre-retrieval access constraints for one knowledge document."""

    allowed_principals: tuple[str, ...] = ()
    denied_tags: tuple[str, ...] = ()
    policy_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_principals",
            tuple(str(item).strip() for item in self.allowed_principals if str(item).strip()),
        )
        object.__setattr__(
            self,
            "denied_tags",
            tuple(str(item).strip() for item in self.denied_tags if str(item).strip()),
        )

    def allows(self, principal: str, tags: Sequence[str]) -> tuple[bool, str]:
        normalized_principal = str(principal or "local").strip()
        if self.allowed_principals and normalized_principal not in self.allowed_principals:
            return False, "principal-not-allowed"
        denied = set(self.denied_tags).intersection(str(tag) for tag in tags)
        if denied:
            return False, "tag-denied:" + ",".join(sorted(denied))
        return True, ""


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    source_id: str
    scope: KnowledgeScope | str
    title: str
    uri: str
    content: str
    media_type: str = "text/markdown"
    version: KnowledgeVersion = field(default_factory=KnowledgeVersion)
    updated_at: float = 0.0
    expires_at: Optional[float] = None
    tags: tuple[str, ...] = ()
    access: KnowledgeAccessPolicy = field(default_factory=KnowledgeAccessPolicy)
    owner: str = ""
    authority_level: str = "reference"
    effective_from: float = 0.0
    supersedes: str = ""
    conflict_group: str = ""
    asset: Optional[AssetEnvelope] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", KnowledgeScope(self.scope))
        object.__setattr__(self, "document_id", str(self.document_id or "").strip())
        object.__setattr__(self, "source_id", str(self.source_id or "").strip())
        object.__setattr__(self, "title", str(self.title or "").strip())
        object.__setattr__(self, "uri", str(self.uri or "").strip())
        content = str(self.content or "")
        object.__setattr__(self, "content", content)
        object.__setattr__(
            self, "tags", tuple(dict.fromkeys(str(item) for item in self.tags if str(item))))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "owner", str(self.owner or "").strip())
        object.__setattr__(
            self,
            "authority_level",
            str(self.authority_level or "reference").strip().lower(),
        )
        object.__setattr__(
            self, "effective_from", max(0.0, float(self.effective_from)))
        object.__setattr__(
            self, "supersedes", str(self.supersedes or "").strip())
        object.__setattr__(
            self, "conflict_group", str(self.conflict_group or "").strip())
        if not self.document_id:
            raise ValueError("knowledge document_id 不能为空")
        if not self.source_id:
            raise ValueError("knowledge source_id 不能为空")
        if not self.uri:
            raise ValueError("knowledge uri 不能为空")
        if not self.content.strip():
            raise ValueError("knowledge content 不能为空")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        version = self.version
        if not version.digest:
            version = KnowledgeVersion(
                version=version.version,
                digest=digest,
                created_at=version.created_at,
            )
        object.__setattr__(self, "version", version)
        if self.asset is None:
            scope = KnowledgeScope(self.scope)
            raw_derived = self.metadata.get("derived_from")
            derived_from = (
                tuple(str(item) for item in raw_derived)
                if isinstance(raw_derived, (list, tuple)) else ()
            )
            object.__setattr__(self, "asset", AssetEnvelope(
                asset_id=self.document_id,
                asset_type="knowledge",
                scope={
                    KnowledgeScope.USER: AssetScope.USER,
                    KnowledgeScope.PROJECT: AssetScope.PROJECT,
                    KnowledgeScope.ORGANIZATION: AssetScope.ORGANIZATION,
                    KnowledgeScope.SESSION: AssetScope.SESSION,
                }[scope],
                owner=self.owner,
                tenant_id=str(self.metadata.get("tenant_id") or ""),
                organization_id=str(
                    self.metadata.get("organization_id") or ""),
                project_id=str(self.metadata.get("project_id") or ""),
                user_id=str(self.metadata.get("user_id") or ""),
                session_id=str(self.metadata.get("session_id") or ""),
                version=version.version or self.digest[:12],
                retention=str(
                    self.metadata.get("retention")
                    or AssetRetention.PROJECT.value),
                expires_at=self.expires_at,
                derived_from=derived_from,
                metadata={"knowledge_scope": scope.value},
            ))

    @property
    def digest(self) -> str:
        return self.version.digest

    def freshness(self, *, now: Optional[float] = None) -> KnowledgeFreshness:
        current = time.time() if now is None else float(now)
        if self.expires_at is None:
            return KnowledgeFreshness.FRESH
        if self.expires_at <= current:
            return KnowledgeFreshness.EXPIRED
        if self.updated_at and current - self.updated_at > max(
                self.expires_at - self.updated_at, 1.0) * 0.8:
            return KnowledgeFreshness.STALE
        return KnowledgeFreshness.FRESH


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    content: str
    ordinal: int = 0
    digest: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        content = str(self.content or "")
        object.__setattr__(self, "chunk_id", str(self.chunk_id or "").strip())
        object.__setattr__(self, "document_id", str(self.document_id or "").strip())
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not self.chunk_id or not self.document_id:
            raise ValueError("knowledge chunk_id/document_id 不能为空")
        if not content.strip():
            raise ValueError("knowledge chunk content 不能为空")
        if not self.digest:
            object.__setattr__(
                self,
                "digest",
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )


@dataclass(frozen=True)
class KnowledgeQuery:
    query: str
    limit: int = 8
    scopes: tuple[KnowledgeScope, ...] = ()
    principal: str = "local"
    tenant_id: str = ""
    organization_id: str = ""
    project_id: str = ""
    session_id: str = ""
    task_id: str = ""
    tags: tuple[str, ...] = ()
    max_tokens: int = 4000
    include_expired: bool = False

    def __post_init__(self) -> None:
        query = " ".join(str(self.query or "").split())
        if not query:
            raise ValueError("knowledge query 不能为空")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "limit", max(1, min(int(self.limit), 50)))
        object.__setattr__(
            self,
            "scopes",
            tuple(KnowledgeScope(item) for item in self.scopes),
        )
        object.__setattr__(
            self, "tags", tuple(dict.fromkeys(str(item) for item in self.tags if str(item))))
        object.__setattr__(self, "principal", str(self.principal or "local"))
        for name in (
            "tenant_id", "organization_id", "project_id",
            "session_id", "task_id",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "max_tokens", max(1, int(self.max_tokens)))


@dataclass(frozen=True)
class KnowledgeHit:
    document: KnowledgeDocument
    chunk: KnowledgeChunk
    score: float
    freshness: KnowledgeFreshness
    access_reason: str = ""


@dataclass(frozen=True)
class KnowledgeSearchResult:
    query: KnowledgeQuery
    hits: tuple[KnowledgeHit, ...] = ()
    issues: tuple[str, ...] = ()
    source_count: int = 0


@dataclass(frozen=True)
class KnowledgeSourceStats:
    source_id: str
    source_kind: KnowledgeSourceKind | str
    scope: KnowledgeScope | str
    documents: int
    chunks: int
    version: str = ""
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", KnowledgeSourceKind(self.source_kind))
        object.__setattr__(self, "scope", KnowledgeScope(self.scope))


class KnowledgeSourcePort(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def source_kind(self) -> KnowledgeSourceKind: ...

    @property
    def scope(self) -> KnowledgeScope: ...

    def collect(self) -> Sequence[KnowledgeDocument]: ...


class KnowledgeRetrieverPort(Protocol):
    def search(self, query: KnowledgeQuery) -> KnowledgeSearchResult: ...


class KnowledgeServicePort(KnowledgeRetrieverPort, Protocol):
    @property
    def source_names(self) -> tuple[str, ...]: ...

    def refresh(self) -> tuple[str, ...]: ...

    def stats(self) -> tuple[KnowledgeSourceStats, ...]: ...


__all__ = [
    "KnowledgeAccessPolicy",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeFreshness",
    "KnowledgeHit",
    "KnowledgeQuery",
    "KnowledgeRetrieverPort",
    "KnowledgeScope",
    "KnowledgeSearchResult",
    "KnowledgeServicePort",
    "KnowledgeSourceKind",
    "KnowledgeSourcePort",
    "KnowledgeSourceStats",
    "KnowledgeVersion",
]
