"""命令 handler 的显式依赖容器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ...contracts.tool import ToolContext
from .ports import (
    CheckpointManagerPort,
    CommandLoopPort,
    MCPReloadPort,
    MCPSupervisorPort,
    ModelSwitchPort,
    PermissionManagerPort,
    PlanManagerPort,
    PluginManagerPort,
    PluginReloadPort,
    RewindPort,
    SessionLifecyclePort,
    ShellJobCommandPort,
    SkillManagerPort,
    SkillReloadPort,
    SubagentManagerCommandPort,
    UsageTrackerPort,
)
from .registry import CommandRegistry
from .types import CommandRequest


@dataclass(frozen=True)
class CommandContext:
    request: CommandRequest
    loop: CommandLoopPort
    command_registry: CommandRegistry
    workspace_root: Path
    tool_context: Optional[ToolContext] = None
    session_path: Optional[str] = None
    sessions_dir: Optional[Path] = None
    mcp_supervisor: Optional[MCPSupervisorPort] = None
    mcp_reload: Optional[MCPReloadPort] = None
    subagents: Optional[SubagentManagerCommandPort] = None
    checkpoints: Optional[CheckpointManagerPort] = None
    usage: Optional[UsageTrackerPort] = None
    model_switch: Optional[ModelSwitchPort] = None
    skills: Optional[SkillManagerPort] = None
    skills_reload: Optional[SkillReloadPort] = None
    plugins: Optional[PluginManagerPort] = None
    plugins_reload: Optional[PluginReloadPort] = None
    session_lifecycle: Optional[SessionLifecyclePort] = None
    permission_manager: Optional[PermissionManagerPort] = None
    plan_manager: Optional[PlanManagerPort] = None
    rewind: Optional[RewindPort] = None
    jobs: Optional[ShellJobCommandPort] = None
