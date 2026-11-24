"""Compatibility command dispatcher for terminal-facing REPL commands.

Business commands are delegated to ``SessionController``; only renderer-local
commands such as ``/thinking`` and ``/tool-output`` are handled here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..agent.turn.loop import AgentLoop
from ..session.commands import CommandRegistry, CommandResult
from ..session.commands.ports import (
    MCPReloadPort,
    MCPSupervisorPort,
    ModelSwitchPort,
    PluginManagerPort,
    PluginReloadPort,
    SkillManagerPort,
    SkillReloadPort,
    SubagentManagerCommandPort,
    UsageTrackerPort,
)
from ..session.controller import SessionController
from ..session.runtime_ports import CheckpointPort
from .render import StreamRenderer


async def _handle_command(
    user_input: str,
    loop: AgentLoop,
    renderer: StreamRenderer,
    *,
    session_path: Optional[str],
    sessions_dir: Optional[Path],
    mcp_supervisor: Optional[MCPSupervisorPort],
    mcp_reload: Optional[MCPReloadPort],
    subagents: Optional[SubagentManagerCommandPort],
    checkpoints: Optional[CheckpointPort],
    usage: Optional[UsageTrackerPort],
    restore_session: Optional[Callable[..., object]],
    workspace_root: Path,
    model_switch: Optional[ModelSwitchPort] = None,
    skills: Optional[SkillManagerPort] = None,
    skills_reload: Optional[SkillReloadPort] = None,
    plugins: Optional[PluginManagerPort] = None,
    plugins_reload: Optional[PluginReloadPort] = None,
    controller: Optional[SessionController] = None,
    command_registry: Optional[CommandRegistry] = None,
    diff_handler: Optional[Callable[[str], Awaitable[bool]]] = None,
) -> CommandResult | bool | None:
    """兼容入口：终端展示命令留在 UI，业务命令委托 SessionController。"""
    if not user_input.startswith("/"):
        return None

    if user_input == "/diff" or user_input.startswith("/diff "):
        arguments = user_input.partition(" ")[2]
        if diff_handler is not None:
            await diff_handler(arguments)
        else:
            print("  （当前前台不支持 diff 审阅器）")
        return False

    # 这两类命令操作的是当前终端 renderer 的展示缓存，不属于会话业务状态。
    if user_input == "/thinking":
        if not renderer.expand_thinking():
            print("  （暂无思考内容）")
        return False
    if user_input == "/tool-output" or user_input.startswith("/tool-output "):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            if not renderer.list_tool_outputs():
                print("  （暂无可展开的工具输出）")
        elif not renderer.expand_tool_output(parts[1].strip()):
            print("  （未找到工具输出；输入 /tool-output 查看编号）")
        return False

    if controller is None:
        controller = SessionController(
            loop,
            session_path=session_path,
            sessions_dir=sessions_dir,
            mcp_supervisor=mcp_supervisor,
            mcp_reload=mcp_reload,
            subagents=subagents,
            checkpoints=checkpoints,
            usage=usage,
            model_switch=model_switch,
            skills=skills,
            skills_reload=skills_reload,
            plugins=plugins,
            plugins_reload=plugins_reload,
            workspace_root=workspace_root,
            command_registry=command_registry,
        )
    result = await controller.execute_command(user_input)
    for line in result.output:
        print(line)
    if result.clear_tool_outputs:
        renderer.clear_tool_outputs()
    if result.run_prompt is not None:
        return result
    return False
