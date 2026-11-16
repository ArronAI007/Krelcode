"""命令 handler 的显式依赖容器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .registry import CommandRegistry
from .types import CommandRequest


@dataclass(frozen=True)
class CommandContext:
    request: CommandRequest
    loop: Any
    command_registry: CommandRegistry
    workspace_root: Path
    session_path: Optional[str] = None
    sessions_dir: Optional[Path] = None
    mcp_supervisor: Any = None
    mcp_reload: Any = None
    subagents: Any = None
    checkpoints: Any = None
    usage: Any = None
    model_switch: Any = None
    skills: Any = None
    skills_reload: Any = None
    plugins: Any = None
    plugins_reload: Any = None
    session_lifecycle: Any = None
