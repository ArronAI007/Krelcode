"""内置业务命令 handler 映射。"""

from __future__ import annotations

from typing import Awaitable, Callable

from ..context import CommandContext
from ..types import CommandResult
from .runtime_ops import (
    handle_agents,
    handle_allow,
    handle_compact,
    handle_context,
    handle_cost,
    handle_mcp,
    handle_model,
    handle_skill,
    handle_skills,
    handle_tools,
    handle_undo,
)
from .session_ops import (
    handle_export,
    handle_help,
    handle_history,
    handle_reset,
    handle_resume,
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
    "mcp": handle_mcp,
    "agents": handle_agents,
    "skills": handle_skills,
    "skill": handle_skill,
    "undo": handle_undo,
    "cost": handle_cost,
    "model": handle_model,
    "allow": handle_allow,
    "context": handle_context,
    "export": handle_export,
    "compact": handle_compact,
    "reset": handle_reset,
}

__all__ = ["BUILTIN_HANDLERS", "CommandHandler"]
