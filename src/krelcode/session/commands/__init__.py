"""统一 slash command 类型、注册表与内置目录。"""

from .builtin import (
    builtin_command_registry,
    builtin_command_specs,
    render_help,
)
from .context import CommandContext
from .registry import CommandConflict, CommandRegistry, normalize_token
from .sources import (
    mcp_prompt_command_specs,
    plugin_command_specs,
    skill_command_specs,
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from .types import (
    CommandKind,
    CommandRequest,
    CommandResult,
    CommandSource,
    CommandSpec,
)

__all__ = [
    "CommandConflict",
    "CommandContext",
    "CommandKind",
    "CommandRequest",
    "CommandRegistry",
    "CommandResult",
    "CommandSource",
    "CommandSpec",
    "builtin_command_registry",
    "builtin_command_specs",
    "normalize_token",
    "render_help",
    "mcp_prompt_command_specs",
    "plugin_command_specs",
    "skill_command_specs",
    "sync_mcp_prompt_commands",
    "sync_plugin_commands",
    "sync_skill_commands",
]
