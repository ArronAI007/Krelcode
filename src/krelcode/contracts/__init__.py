"""Shared, dependency-light contracts between runtime layers."""

from .runtime import PermissionMode, RunMode, RuntimeSnapshot
from .safety import SandboxPort
from .tool import (
    CommandDescriptor,
    Tool,
    ToolCatalog,
    ToolCatalogMatch,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    ToolServices,
    ToolSource,
    ToolTier,
    TurnAbortSignal,
)

__all__ = [
    "CommandDescriptor",
    "PermissionMode",
    "RunMode",
    "RuntimeSnapshot",
    "SandboxPort",
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
