"""ToolSearch: load deferred tool schemas from the filtered turn catalogue."""

from __future__ import annotations

from typing import Any, Dict

from ..base import ToolContext, ToolResult, ToolTier


class ToolSearchTool:
    """Search and load tools without exposing the mutable global registry."""

    name = "tool_search"
    read_only = True
    tool_tier = ToolTier.CORE
    search_terms = (
        "tool", "tools", "catalog", "capability", "capabilities", "search",
    )
    description = (
        "Search the current turn's dynamically filtered tool catalogue and load "
        "matching full schemas. Use `*` to list available tools. Loaded tools are "
        "callable from the next model step; disabled, offline MCP, run-mode-blocked "
        "and permission-blocked tools are never returned."
    )
    prompt = """\
Use tool_search only when the currently advertised tools cannot perform the task
or a relevant deferred capability is likely to exist. Search by capability, load
the best match when appropriate, and call a newly loaded tool on the next model
step because its full schema is not available in the requesting step. Do not
search repeatedly for a tool that is already loaded and usable.
"""
    when_to_use = (
        "The active tool directory lacks a capability the task appears to require.",
        "A deferred or discoverable integration may provide the needed operation.",
    )
    when_not_to_use = (
        "An advertised tool already performs the operation.",
        "The search would be broad exploration without a concrete capability need.",
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": (
                    "Tool name, capability, or keywords to search for; use * to list."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Maximum matches to return; default 5.",
            },
            "load": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Load full schemas for matching tools. They become callable "
                    "on the next model step."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        catalog = ctx.tool_catalog
        if catalog is None:
            return ToolResult(
                content="ToolSearch unavailable: no active turn tool catalogue",
                is_error=True,
            )
        query = " ".join(str(args.get("query") or "").split())
        if not query:
            return ToolResult(
                content="tool_search requires a non-empty query", is_error=True)
        try:
            limit = max(1, min(int(args.get("limit") or 5), 10))
        except (TypeError, ValueError) as exc:
            return ToolResult(
                content=f"invalid tool_search limit: {exc}", is_error=True)
        load = bool(args.get("load", True))
        matches = catalog.search_catalog(query, limit=limit)
        if not matches:
            return ToolResult(
                content=(
                    f"No eligible tool matched {query!r}. Search only covers tools "
                    "allowed by the current permission mode, run mode, MCP health, "
                    "feature switches and user disable list."
                ),
                data={"query": query, "matches": [], "loaded": []},
            )

        loaded = catalog.load_tools([item.name for item in matches]) if load else ()
        lines = [f"ToolSearch: {query}"]
        for item in matches:
            state = "loaded" if item.name in loaded or item.loaded else item.tier.value
            category = "read-only" if item.read_only else "mutating"
            lines.append(
                f"- {item.name} [{state}, {category}, {item.source}]: "
                f"{item.description}")
        if loaded:
            lines.append(
                "Loaded full schema(s) for the next model step: "
                + ", ".join(loaded))
            lines.append(
                "Do not call a newly loaded tool in the same request that loaded it.")
        elif load:
            lines.append("No new full schemas were loaded.")
        else:
            lines.append(
                "Search-only result. Repeat with load=true to make a result callable.")
        return ToolResult(
            content="\n".join(lines),
            data={
                "query": query,
                "matches": [
                    {
                        "name": item.name,
                        "tier": item.tier.value,
                        "source": item.source,
                        "read_only": item.read_only,
                        "loaded": item.name in loaded or item.loaded,
                    }
                    for item in matches
                ],
                "loaded": list(loaded),
            },
        )
