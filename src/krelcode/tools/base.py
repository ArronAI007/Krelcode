"""Compatibility surface for the shared Tool contracts.

The canonical DTOs live in :mod:`krelcode.contracts.tool`.  ``tools.base`` keeps
the historical import path and supplies the fail-safe default Sandbox for tool
callers that construct an execution context directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
    ToolContext as ContractToolContext,
    ToolDescriptor,
    ToolResult,
    ToolServices,
    ToolSource,
    ToolTier,
    TurnAbortSignal,
)
from ..contracts.safety import SandboxPort
from ..safety.sandbox import ExecutionSandbox


@dataclass
class ToolContext(ContractToolContext):
    """Historical ToolContext with the concrete default Sandbox injected.

    New cross-layer code should depend on ``contracts.tool.ToolContext``.  This
    compatibility subclass keeps direct tool construction fail-safe without
    making the contract layer import the Safety adapter.
    """

    sandbox: Optional[SandboxPort] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sandbox_enabled and self.sandbox is None:
            self.sandbox = ExecutionSandbox(self.workspace_root)


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
