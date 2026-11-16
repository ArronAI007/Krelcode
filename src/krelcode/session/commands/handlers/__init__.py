"""内置业务命令 handler 映射。"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..context import CommandContext
from ..types import CommandResult, CommandSource
from .runtime_ops import (
    handle_agents,
    handle_allow,
    handle_compact,
    handle_context,
    handle_cost,
    handle_mcp,
    handle_mcp_prompt_command,
    handle_model,
    handle_permissions,
    handle_plugins,
    handle_skill,
    handle_skill_command,
    handle_skills,
    handle_tools,
    handle_undo,
)
from .session_ops import (
    handle_delete,
    handle_export,
    handle_fork,
    handle_help,
    handle_history,
    handle_reset,
    handle_rename,
    handle_resume,
    handle_rewind,
    handle_session,
    handle_sessions,
)

CommandHandler = Callable[[CommandContext], Awaitable[CommandResult]]

BUILTIN_HANDLERS: dict[str, CommandHandler] = {
    "help": handle_help,
    "history": handle_history,
    "tools": handle_tools,
    "session": handle_session,
    "sessions": handle_sessions,
    "resume": handle_resume,
    "fork": handle_fork,
    "rewind": handle_rewind,
    "rename": handle_rename,
    "delete": handle_delete,
    "mcp": handle_mcp,
    "agents": handle_agents,
    "skills": handle_skills,
    "plugins": handle_plugins,
    "skill": handle_skill,
    "undo": handle_undo,
    "cost": handle_cost,
    "model": handle_model,
    "allow": handle_allow,
    "permissions": handle_permissions,
    "context": handle_context,
    "export": handle_export,
    "compact": handle_compact,
    "reset": handle_reset,
}

async def execute_prompt_command(ctx: CommandContext) -> CommandResult:
    """按 command source 选择动态 prompt handler。"""
    spec = ctx.command_registry.resolve(ctx.request.name)
    if spec is not None and spec.source is CommandSource.SKILL:
        return await handle_skill_command(ctx)
    if spec is not None and spec.source is CommandSource.MCP:
        return await handle_mcp_prompt_command(ctx)
    return CommandResult(
        handled=True,
        output=(f"  （未知 prompt 命令: /{ctx.request.name}）",),
    )


__all__ = ["BUILTIN_HANDLERS", "CommandHandler", "execute_prompt_command"]
