"""Identity-filtered Knowledge ContextSource."""

from __future__ import annotations

from typing import Sequence

from ...contracts.knowledge import (
    KnowledgeHit,
    KnowledgeQuery,
    KnowledgeRetrieverPort,
    KnowledgeScope,
)
from ...core.evidence import (
    EvidenceRecord,
    EvidenceSource,
    evidence_metadata,
    render_knowledge_evidence_set,
)
from ...core.tokens import estimate_text_tokens
from .context_contracts import (
    CATEGORY_KNOWLEDGE,
    TRUST_UNTRUSTED,
    ContextLayer,
    ContextTokenContribution,
)
from .context_request import ContextRequest


class KnowledgeContextSource:
    """Expose access-filtered knowledge as non-instruction evidence."""

    name = "knowledge"

    def __init__(self, retriever: KnowledgeRetrieverPort) -> None:
        self.retriever = retriever

    def collect(self, request: "ContextRequest") -> Sequence[ContextLayer]:
        query_text = _knowledge_query_text(request)
        if not query_text:
            return ()
        result = self.retriever.search(KnowledgeQuery(
            query=query_text,
            limit=4,
            principal=request.principal.principal_id,
            tenant_id=request.principal.tenant_id,
            organization_id=request.principal.organization_id,
            project_id=request.project_id,
            session_id=request.session_id,
            task_id=request.task_id,
            max_tokens=1_400,
            include_expired=False,
        ))
        if not result.hits:
            return ()
        records = tuple(
            _knowledge_evidence_record(hit) for hit in result.hits)
        metadata = evidence_metadata(
            records,
            origin="knowledge_index",
            extra={
                "knowledge_hit_ids": tuple(
                    hit.chunk.chunk_id for hit in result.hits),
                "knowledge_document_ids": tuple(
                    hit.document.document_id for hit in result.hits),
                "knowledge_sources": tuple(sorted({
                    hit.document.source_id for hit in result.hits})),
                "knowledge_tokens": estimate_text_tokens(
                    "\n".join(record.content for record in records)),
                "knowledge_hits": tuple({
                    "id": hit.chunk.chunk_id,
                    "document_id": hit.document.document_id,
                    "source": hit.document.source_id,
                    "scope": KnowledgeScope(hit.document.scope).value,
                    "version": hit.document.version.version,
                    "owner": hit.document.owner,
                    "authority_level": hit.document.authority_level,
                    "effective_from": hit.document.effective_from,
                    "conflict_group": hit.document.conflict_group,
                    "freshness": hit.freshness.value,
                    "score": hit.score,
                    "tokens": estimate_text_tokens(hit.chunk.content),
                } for hit in result.hits),
                "knowledge_issues": result.issues,
            },
        )
        content = render_knowledge_evidence_set(
            records, origin="knowledge_index")
        return (ContextLayer(
            id="knowledge:retrieved",
            source=self.name,
            category=CATEGORY_KNOWLEDGE,
            content=content,
            trust=TRUST_UNTRUSTED,
            origin="knowledge_index",
            source_type="knowledge",
            source_id="knowledge:retrieved",
            scope="mixed",
            version=",".join(sorted({
                hit.document.version.version
                for hit in result.hits
                if hit.document.version.version
            })),
            permission_basis="knowledge_access_policy+asset_scope",
            selected_reason="goal lexical retrieval",
            transform="retrieved_evidence",
            priority=70,
            cacheable=False,
            compactable=True,
            metadata=metadata,
            token_contributions=(ContextTokenContribution(
                "knowledge:retrieved",
                CATEGORY_KNOWLEDGE,
                estimate_text_tokens(content),
            ),),
        ),)


def _knowledge_query_text(request: "ContextRequest") -> str:
    if request.application_request is not None:
        goal = str(request.application_request.goal or "").strip()
        if goal:
            return goal
    for message in reversed(request.history):
        if message.role == "user":
            return str(message.content or "").strip()
    return ""


def _knowledge_evidence_record(hit: KnowledgeHit) -> EvidenceRecord:
    document = hit.document
    return EvidenceRecord(
        id=hit.chunk.chunk_id,
        source=EvidenceSource.KNOWLEDGE,
        content=hit.chunk.content,
        scope=KnowledgeScope(document.scope).value,
        kind="knowledge",
        trust="untrusted",
        confidence=min(0.95, 0.3 + hit.score / 20.0),
        requires_validation=True,
        origin=document.source_id,
        metadata={
            "document_id": document.document_id,
            "title": document.title,
            "uri": document.uri,
            "version": document.version.version,
            "digest": document.digest,
            "owner": document.owner,
            "authority_level": document.authority_level,
            "effective_from": document.effective_from,
            "supersedes": document.supersedes,
            "conflict_group": document.conflict_group,
            "freshness": hit.freshness.value,
            "source_kind": document.metadata.get("source_kind", ""),
            "tags": list(document.tags),
        },
    )


__all__ = ["KnowledgeContextSource"]
