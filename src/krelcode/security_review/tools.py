"""Dedicated local read-only tool pool for Security Review."""

from __future__ import annotations

from typing import List

from ..contracts.tool import Tool
from ..tools.builtin.find_refs import FindRefsTool
from ..tools.builtin.glob import GlobTool
from ..tools.builtin.grep import GrepTool
from ..tools.builtin.read_file import ReadFileTool
from ..tools.builtin.repo_map import RepoMapTool
from ..tools.builtin.tool_search import ToolSearchTool
from ..tools.registry import ToolRegistry


SECURITY_REVIEW_TOOL_NAMES = frozenset({
    "read_file",
    "grep",
    "glob",
    "find_refs",
    "repo_map",
    "tool_search",
})


def security_review_tools() -> List[Tool]:
    """Return a local tool set with no write, shell, network or MCP capability."""
    tools: List[Tool] = [
        ReadFileTool(),
        GrepTool(),
        GlobTool(),
        FindRefsTool(),
        RepoMapTool(),
        ToolSearchTool(),
    ]
    assert {tool.name for tool in tools} == set(SECURITY_REVIEW_TOOL_NAMES)
    assert all(bool(getattr(tool, "read_only", False)) for tool in tools)
    return tools


def build_security_review_registry() -> ToolRegistry:
    """Build the isolated execution registry used by the headless runner."""
    return ToolRegistry(security_review_tools())


__all__ = [
    "SECURITY_REVIEW_TOOL_NAMES",
    "build_security_review_registry",
    "security_review_tools",
]
