"""Tool and MCP scope narrowing for delegated agents."""

from __future__ import annotations

from typing import List, Tuple

from ..contracts.tool import Tool


def mcp_server_name(tool: Tool) -> str:
    client = getattr(tool, "client", None)
    return str(getattr(client, "name", "") or "")


def restrict_mcp_tools(tools: List[Tool], allowed: set[str]) -> List[Tool]:
    restricted: List[Tool] = []
    for tool in tools:
        server = mcp_server_name(tool)
        if server and server not in allowed:
            continue
        restricted.append(tool)
    return restricted


def restrict_memory_tools(
    tools: List[Tool], memory_ids: Tuple[str, ...],
) -> List[Tool]:
    restricted = []
    allowed = frozenset(memory_ids)
    for tool in tools:
        if tool.name != "memory_search":
            restricted.append(tool)
            continue
        factory = getattr(tool, "restricted_to", None)
        if callable(factory):
            restricted.append(factory(allowed))
        elif allowed:
            restricted.append(tool)
    return restricted


__all__ = [
    "mcp_server_name",
    "restrict_mcp_tools",
    "restrict_memory_tools",
]
