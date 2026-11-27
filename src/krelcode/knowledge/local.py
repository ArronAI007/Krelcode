"""Zero-dependency local knowledge source and deterministic lexical index."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from krelcode.contracts.knowledge import (
    KnowledgeAccessPolicy,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeFreshness,
    KnowledgeHit,
    KnowledgeQuery,
    KnowledgeScope,
    KnowledgeSearchResult,
    KnowledgeServicePort,
    KnowledgeSourceKind,
    KnowledgeSourceStats,
    KnowledgeVersion,
)
from krelcode.core.redact import SecretRedactor

MAX_SOURCE_FILES = 500
MAX_DOCUMENT_BYTES = 1_000_000
MAX_CHUNK_CHARS = 1_400
TEXT_SUFFIXES = frozenset({".md", ".mdx", ".txt", ".rst"})
_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass(frozen=True)
class _SourceRoot:
    source_id: str
    kind: KnowledgeSourceKind
    scope: KnowledgeScope
    root: Path


class LocalKnowledgeSource:
    """File-backed knowledge source for one local scope root."""

    def __init__(
        self,
        source_id: str,
        root: Path,
        *,
        scope: KnowledgeScope = KnowledgeScope.PROJECT,
        kind: KnowledgeSourceKind = KnowledgeSourceKind.LOCAL_DOCUMENTS,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self._source_id = source_id
        self.root = Path(root)
        self.scope = KnowledgeScope(scope)
        self.source_kind = KnowledgeSourceKind(kind)
        self.redactor = redactor

    @property
    def source_id(self) -> str:
        return self._source_id

    def collect(self) -> Sequence[KnowledgeDocument]:
        if not self.root.is_dir():
            return ()
        documents: list[KnowledgeDocument] = []
        files = sorted(
            (
                path
                for path in self.root.rglob("*")
                if path.is_file()
                and path.suffix.lower() in TEXT_SUFFIXES
                and not path.is_symlink()
            ),
            key=lambda item: item.as_posix(),
        )[:MAX_SOURCE_FILES]
        for path in files:
            try:
                if path.stat().st_size > MAX_DOCUMENT_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if self.redactor is not None:
                text = self.redactor.redact(text)
            if not text.strip():
                continue
            metadata = _sidecar_metadata(path)
            relative = path.relative_to(self.root).as_posix()
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            title = str(
                metadata.get("title")
                or _markdown_title(text)
                or path.stem.replace("-", " ").replace("_", " ")
            ).strip()
            tags = metadata.get("tags")
            acl = metadata.get("acl")
            expires_at = metadata.get("expires_at")
            denied_tags = metadata.get("denied_tags", ())
            documents.append(KnowledgeDocument(
                document_id=f"{self.source_id}:{relative}:{digest[:12]}",
                source_id=self.source_id,
                scope=self.scope,
                title=title,
                uri=path.resolve().as_uri(),
                content=text,
                version=KnowledgeVersion(
                    version=str(metadata.get("version") or digest[:12]),
                    digest=digest,
                    created_at=(
                        _optional_float(metadata.get("created_at"))
                        or path.stat().st_mtime
                    ),
                ),
                updated_at=(
                    _optional_float(metadata.get("updated_at"))
                    or path.stat().st_mtime
                ),
                expires_at=_optional_float(expires_at),
                tags=(
                    tuple(str(item) for item in tags)
                    if isinstance(tags, list) else ()
                ),
                access=KnowledgeAccessPolicy(
                    allowed_principals=(
                        tuple(str(item) for item in acl)
                        if isinstance(acl, list) else ()
                    ),
                    denied_tags=(
                        tuple(str(item) for item in denied_tags)
                        if isinstance(denied_tags, list) else ()
                    ),
                    policy_version=str(metadata.get("policy_version") or ""),
                ),
                metadata={
                    "relative_path": relative,
                    "source_root": str(self.root),
                    "source_kind": self.source_kind.value,
                    **metadata,
                },
            ))
        return tuple(documents)


class LocalKnowledgeBase(KnowledgeServicePort):
    """Multi-root local knowledge base with deterministic lexical retrieval."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        organization_root: Optional[Path] = None,
        redactor: Optional[SecretRedactor] = None,
        auto_refresh: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = (
            Path(home).expanduser().resolve()
            if home is not None else Path.home().expanduser().resolve()
        )
        self.redactor = redactor
        self._sources: tuple[LocalKnowledgeSource, ...] = self._build_sources(
            organization_root)
        self._documents: tuple[KnowledgeDocument, ...] = ()
        self._chunks: tuple[KnowledgeChunk, ...] = ()
        self._document_by_id: dict[str, KnowledgeDocument] = {}
        self.last_issues: tuple[str, ...] = ()
        if auto_refresh:
            self.refresh()

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(source.source_id for source in self._sources)

    def refresh(self) -> tuple[str, ...]:
        documents: list[KnowledgeDocument] = []
        chunks: list[KnowledgeChunk] = []
        issues: list[str] = []
        for source in self._sources:
            try:
                collected = source.collect()
            except Exception as exc:
                issues.append(f"{source.source_id}: {type(exc).__name__}")
                continue
            documents.extend(collected)
            for document in collected:
                chunks.extend(_chunk_document(document))
        self._documents = tuple(documents)
        self._chunks = tuple(chunks)
        self._document_by_id = {
            document.document_id: document for document in self._documents}
        self.last_issues = tuple(issues)
        return self.last_issues

    def stats(self) -> tuple[KnowledgeSourceStats, ...]:
        results: list[KnowledgeSourceStats] = []
        for source in self._sources:
            documents = tuple(
                document for document in self._documents
                if document.source_id == source.source_id
            )
            document_ids = {document.document_id for document in documents}
            chunks = sum(
                1 for chunk in self._chunks if chunk.document_id in document_ids)
            version = documents[0].version.version if documents else ""
            updated_at = max(
                (document.updated_at for document in documents), default=0.0)
            results.append(KnowledgeSourceStats(
                source_id=source.source_id,
                source_kind=source.source_kind,
                scope=source.scope,
                documents=len(documents),
                chunks=chunks,
                version=version,
                updated_at=updated_at,
            ))
        return tuple(results)

    def search(self, query: KnowledgeQuery) -> KnowledgeSearchResult:
        terms = _tokens(query.query)
        if not terms:
            return KnowledgeSearchResult(query=query, issues=("empty-query",))
        allowed_scopes = set(query.scopes)
        required_tags = set(query.tags)
        candidates: list[tuple[KnowledgeHit, int]] = []
        issues: list[str] = []
        now = time.time()
        for chunk in self._chunks:
            document = self._document_by_id.get(chunk.document_id)
            if document is None:
                continue
            if allowed_scopes and document.scope not in allowed_scopes:
                continue
            if required_tags and not required_tags.issubset(document.tags):
                continue
            freshness = document.freshness(now=now)
            if freshness is KnowledgeFreshness.EXPIRED and not query.include_expired:
                continue
            allowed, reason = document.access.allows(
                query.principal, document.tags)
            if not allowed:
                issues.append(f"{document.document_id}:{reason}")
                continue
            score = _score(terms, document.title, chunk.content)
            if score <= 0:
                continue
            candidates.append((
                KnowledgeHit(
                    document=document,
                    chunk=chunk,
                    score=score,
                    freshness=freshness,
                ),
                _estimate_tokens(chunk.content),
            ))
        candidates.sort(key=lambda item: (-item[0].score, item[0].chunk.chunk_id))
        hits: list[KnowledgeHit] = []
        used_tokens = 0
        for hit, tokens in candidates:
            if used_tokens + tokens > query.max_tokens and hits:
                break
            used_tokens += tokens
            hits.append(hit)
            if len(hits) >= query.limit:
                break
        return KnowledgeSearchResult(
            query=query,
            hits=tuple(hits),
            issues=tuple(issues + list(self.last_issues)),
            source_count=len({item.document.source_id for item in hits}),
        )

    def _build_sources(
        self,
        organization_root: Optional[Path],
    ) -> tuple[LocalKnowledgeSource, ...]:
        sources = [
            LocalKnowledgeSource(
                "local-project",
                self.workspace_root / ".krelcode" / "knowledge",
                scope=KnowledgeScope.PROJECT,
                redactor=self.redactor,
            ),
            LocalKnowledgeSource(
                "local-user",
                self.home / ".krelcode" / "knowledge",
                scope=KnowledgeScope.USER,
                redactor=self.redactor,
            ),
        ]
        if organization_root is not None:
            sources.append(LocalKnowledgeSource(
                "local-organization",
                Path(organization_root),
                scope=KnowledgeScope.ORGANIZATION,
                redactor=self.redactor,
            ))
        return tuple(sources)


def _sidecar_metadata(path: Path) -> dict[str, object]:
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    if not sidecar.is_file():
        return {}
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _markdown_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _chunk_document(document: KnowledgeDocument) -> tuple[KnowledgeChunk, ...]:
    sections = re.split(r"(?m)(?=^#{1,6}\s)", document.content)
    chunks: list[KnowledgeChunk] = []
    ordinal = 0
    for section in sections:
        section = section.strip()
        if not section:
            continue
        for offset in range(0, len(section), MAX_CHUNK_CHARS):
            content = section[offset:offset + MAX_CHUNK_CHARS].strip()
            if not content:
                continue
            ordinal += 1
            chunks.append(KnowledgeChunk(
                chunk_id=f"{document.document_id}#{ordinal}",
                document_id=document.document_id,
                content=content,
                ordinal=ordinal,
                metadata={
                    "title": document.title,
                    "uri": document.uri,
                    "scope": KnowledgeScope(document.scope).value,
                    "version": document.version.version,
                    "tags": document.tags,
                },
            ))
    return tuple(chunks)


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(
        token.lower() for token in _TOKEN_RE.findall(str(text or ""))
        if len(token) > 1
    )


def _score(terms: Sequence[str], title: str, content: str) -> float:
    title_lower = title.lower()
    content_lower = content.lower()
    score = 0.0
    for term in terms:
        title_hits = title_lower.count(term)
        content_hits = content_lower.count(term)
        if not title_hits and not content_hits:
            continue
        score += title_hits * 4.0 + min(8, content_hits)
    return score


def _estimate_tokens(text: str) -> int:
    return max(1, len(str(text or "")) // 4)


def _optional_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["LocalKnowledgeBase", "LocalKnowledgeSource"]
