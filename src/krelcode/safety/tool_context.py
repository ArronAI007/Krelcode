"""Concrete ToolContext adapter that supplies a fail-safe default Sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..contracts.safety import SandboxPort
from ..contracts.tool import ToolContext as ContractToolContext
from .sandbox import ExecutionSandbox


@dataclass
class ToolContext(ContractToolContext):
    """Historical construction behavior backed by the canonical contract DTO."""

    sandbox: Optional[SandboxPort] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sandbox_enabled and self.sandbox is None:
            self.sandbox = ExecutionSandbox(self.workspace_root)


__all__ = ["ToolContext"]
