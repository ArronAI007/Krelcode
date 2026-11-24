"""Compatibility surface for shared Tool contracts and the legacy adapter."""

from __future__ import annotations

from ..contracts.tool import (
    CommandDescriptor,
    InputRequester,
    OutputEmitter,
    PermissionMode,
    PlanEmitter,
    ProgressEmitter,
    RunMode,
    RuntimeSnapshot,
    Tool,
    ToolCatalog,
    ToolCatalogMatch,
    ToolDescriptor,
    ToolResult,
    ToolServices,
    ToolSource,
    ToolTier,
    TurnAbortSignal,
)
from ..safety.tool_context import ToolContext

__all__ = [
    "CommandDescriptor",
    "InputRequester",
    "OutputEmitter",
    "PermissionMode",
    "PlanEmitter",
    "ProgressEmitter",
    "RunMode",
    "RuntimeSnapshot",
    "Tool",
    "ToolCatalog",
    "ToolCatalogMatch",
    "ToolContext",
    "ToolDescriptor",
    "ToolResult",
    "ToolServices",
    "ToolSource",
    "ToolTier",
    "TurnAbortSignal",
]
