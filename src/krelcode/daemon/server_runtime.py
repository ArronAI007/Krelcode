"""Runtime factories and DTOs for daemon-owned agents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from ..contracts.runtime import (
    RuntimeResourceRegistryPort,
    RuntimeTaskRuntimePort,
)
from ..contracts.tool import ToolContext
from ..core.events import EventBus
from ..core.redact import SecretRedactor


@dataclass(frozen=True)
class DaemonAgentRuntimeRequest:
    """Inputs needed by the composition root to build daemon-owned agents."""

    workspace_root: Path
    config_path: Optional[Path]
    config: Dict[str, Any]
    no_sandbox: bool = False
    sandbox_backend: Optional[str] = None
    sandbox_network: Optional[bool] = None
    auto_approve: bool = False
    permission_mode: Optional[str] = None
    log: Callable[[str], None] = print


@dataclass(frozen=True)
class DaemonAgentRuntime:
    """Concrete agent runtime returned by an injected composition factory."""

    task_runtime: RuntimeTaskRuntimePort
    resources: RuntimeResourceRegistryPort
    bus: EventBus
    agents: Any
    tool_context: Optional[ToolContext] = None
    redactor: Optional[SecretRedactor] = None
    mcp_supervisor: Any = None
    agent_runtime: Any = None
    loop: Optional[object] = None


DaemonAgentRuntimeFactory = Callable[
    [DaemonAgentRuntimeRequest], Awaitable[DaemonAgentRuntime]
]


__all__ = [
    "DaemonAgentRuntime",
    "DaemonAgentRuntimeFactory",
    "DaemonAgentRuntimeRequest",
]
