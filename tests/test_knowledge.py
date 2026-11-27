"""Knowledge source, retrieval, access and evidence integration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.context.context_contracts import CATEGORY_KNOWLEDGE
from krelcode.agent.context.context_source import ContextRequest, KnowledgeContextSource
from krelcode.contracts.application import ApplicationRequestFactory
from krelcode.contracts.knowledge import KnowledgeQuery
from krelcode.contracts.tool import ToolContext
from krelcode.core.evidence import EvidenceSource
from krelcode.core.messages import Message
from krelcode.knowledge import KnowledgeSearchTool, LocalKnowledgeBase


def write_doc(
    root: Path,
    name: str,
    text: str,
    *,
    acl: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    expires_at: float | None = None,
) -> None:
    directory = root / ".krelcode" / "knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps({
            "title": name,
            "acl": list(acl),
            "tags": list(tags),
            "expires_at": expires_at,
        }),
        encoding="utf-8",
    )


def test_local_knowledge_search_enforces_scope_acl_and_freshness(tmp_path: Path) -> None:
    write_doc(
        tmp_path,
        "runbook.md",
        "# Release runbook\n\nUse the verified rollback procedure.",
        tags=("release",),
    )
    write_doc(
        tmp_path,
        "secret.md",
        "# Secret runbook\n\nOnly release operators can read this rollback.",
        acl=("alice",),
    )
    write_doc(
        tmp_path,
        "old.md",
        "# Old runbook\n\nExpired rollback instructions.",
        expires_at=1.0,
    )
    base = LocalKnowledgeBase(tmp_path, home=tmp_path / "home")

    result = base.search(KnowledgeQuery(
        query="rollback",
        principal="bob",
        limit=10,
    ))

    titles = [hit.document.title for hit in result.hits]
    assert "runbook.md" in titles
    assert "secret.md" not in titles
    assert "old.md" not in titles
    assert any("principal-not-allowed" in issue for issue in result.issues)
    assert base.stats()[0].documents == 3


def test_knowledge_context_uses_explicit_evidence_channel(tmp_path: Path) -> None:
    write_doc(
        tmp_path,
        "product.md",
        "# Product policy\n\nEnterprise exports require an audit record.",
    )
    base = LocalKnowledgeBase(tmp_path, home=tmp_path / "home")
    source = KnowledgeContextSource(base)
    request = ContextRequest(
        turn_id="turn-1",
        step=1,
        history=(Message.user("What is the enterprise export policy?"),),
        workspace_root=tmp_path,
        application_request=ApplicationRequestFactory.from_input_source(
            "repl",
            goal="What is the enterprise export policy?",
        ),
    )

    layers = source.collect(request)

    assert len(layers) == 1
    assert layers[0].category == CATEGORY_KNOWLEDGE
    assert layers[0].trust == "untrusted"
    assert "[Evidence: Knowledge]" in layers[0].content
    assert layers[0].metadata["evidence_sources"] == (
        EvidenceSource.KNOWLEDGE.value,)
    assert layers[0].metadata["instruction"] is False


def test_knowledge_search_tool_is_read_only_evidence(tmp_path: Path) -> None:
    write_doc(
        tmp_path,
        "runbook.md",
        "# Release runbook\n\nRollback requires a database snapshot.",
    )
    base = LocalKnowledgeBase(tmp_path, home=tmp_path / "home")
    tool = KnowledgeSearchTool(base)

    result = asyncio.run(tool.execute(
        {"query": "rollback"},
        ToolContext(workspace_root=tmp_path),
    ))

    assert result.is_error is False
    assert "[Evidence:" in result.content
    assert "Knowledge" in result.content
    assert result.data["count"] == 1
    assert tool.read_only is True
