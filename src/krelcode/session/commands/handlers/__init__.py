"""内置业务命令 handler 映射。"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..context import CommandContext
from ..types import CommandResult, CommandSource
from .memory_ops import handle_memory
from .prompt_surface_ops import handle_prompts
from .security_ops import handle_security_review
from .command_ops import (
    handle_agents,
    handle_commands,
    handle_jobs,
    handle_mcp,
    handle_statusline,
    handle_tools,
)
from .configuration_ops import (
    handle_allow,
    handle_cost,
    handle_lsp,
    handle_model,
    handle_output_style,
    handle_permissions,
    handle_undo,
    handle_web,
)
from .context_ops import handle_compact, handle_context
from .extension_ops import (
    handle_custom_command,
    handle_extensions,
    handle_mcp_prompt_command,
    handle_plugin_command,
    handle_plugins,
    handle_skill,
    handle_skill_command,
    handle_skills,
)
from .prompt_release_ops import handle_prompt_release, handle_telemetry
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
    "commands": handle_commands,
    "session": handle_session,
    "sessions": handle_sessions,
    "resume": handle_resume,
    "fork": handle_fork,
    "rewind": handle_rewind,
    "rename": handle_rename,
    "delete": handle_delete,
    "mcp": handle_mcp,
    "agents": handle_agents,
    "jobs": handle_jobs,
    "lsp": handle_lsp,
    "skills": handle_skills,
    "plugins": handle_plugins,
    "extensions": handle_extensions,
    "skill": handle_skill,
    "undo": handle_undo,
    "cost": handle_cost,
    "model": handle_model,
    "output-style": handle_output_style,
    "statusline": handle_statusline,
    "allow": handle_allow,
    "permissions": handle_permissions,
    "context": handle_context,
    "prompts": handle_prompts,
    "prompt-release": handle_prompt_release,
    "telemetry": handle_telemetry,
    "security-review": handle_security_review,
    "memory": handle_memory,
    "web": handle_web,
    "export": handle_export,
    "compact": handle_compact,
    "reset": handle_reset,
}

async def execute_prompt_command(ctx: CommandContext) -> CommandResult:
    """按 command source 选择动态 prompt handler。"""
    spec = ctx.command_registry.resolve(ctx.request.name)
    if spec is not None and spec.source is CommandSource.SKILL:
        return await handle_skill_command(ctx)
    if spec is not None and spec.source is CommandSource.CUSTOM:
        return await handle_custom_command(ctx)
    if spec is not None and spec.source is CommandSource.PLUGIN:
        return await handle_plugin_command(ctx)
    if spec is not None and spec.source is CommandSource.MCP:
        return await handle_mcp_prompt_command(ctx)
    return CommandResult(
        handled=True,
        output=(f"  （未知 prompt 命令: /{ctx.request.name}）",),
    )


__all__ = ["BUILTIN_HANDLERS", "CommandHandler", "execute_prompt_command"]
