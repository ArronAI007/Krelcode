"""Slash commands for the versioned local knowledge base."""

from __future__ import annotations

from ....contracts.knowledge import (
    KnowledgeQuery,
    KnowledgeScope,
    KnowledgeServicePort,
)
from ..context import CommandContext
from ..types import CommandResult
from .handler_utils import _ok


async def handle_knowledge(ctx: CommandContext) -> CommandResult:
    service = ctx.loop.services.context.knowledge
    if service is None:
        return _ok("  （Knowledge 未启用）")
    arguments = ctx.request.arguments.strip()
    sub, _, rest = arguments.partition(" ")
    if sub == "refresh":
        issues = service.refresh()
        lines = ["  [knowledge] 已刷新"]
        lines.extend(_stats_lines(service))
        lines.extend(f"  [knowledge] issue: {item}" for item in issues)
        return _ok(*lines)
    if sub == "search":
        query = rest.strip()
        if not query:
            return _ok("  用法: /knowledge search <query>")
        result = service.search(KnowledgeQuery(query=query, limit=10))
        if not result.hits:
            return _ok("  （没有匹配知识）")
        lines = []
        for hit in result.hits:
            lines.append(
                f"  {hit.document.title} · {KnowledgeScope(hit.document.scope).value}"
                f" · v{hit.document.version.version}"
                f" · {hit.freshness.value}"
                f" · {hit.document.source_id}")
            lines.append(f"    {hit.chunk.content.splitlines()[0][:180]}")
        return _ok(*lines)
    if sub and sub != "list":
        return _ok("  用法: /knowledge [list|refresh|search <query>]")
    return _ok(*_stats_lines(service))


def _stats_lines(service: KnowledgeServicePort) -> list[str]:
    lines = ["  [knowledge] sources"]
    for stat in service.stats():
        lines.append(
            f"  {stat.source_id} · {KnowledgeScope(stat.scope).value}"
            f" · documents {stat.documents} · chunks {stat.chunks}"
            f" · version {stat.version or '-'}")
    return lines


__all__ = ["handle_knowledge"]
