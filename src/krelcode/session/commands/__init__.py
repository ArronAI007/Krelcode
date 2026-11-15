"""统一 slash command 类型、注册表与内置目录。"""

from .builtin import (
    builtin_command_registry,
    builtin_command_specs,
    render_help,
)
from .registry import CommandConflict, CommandRegistry, normalize_token
from .types import CommandKind, CommandSource, CommandSpec

__all__ = [
    "CommandConflict",
    "CommandKind",
    "CommandRegistry",
    "CommandSource",
    "CommandSpec",
    "builtin_command_registry",
    "builtin_command_specs",
    "normalize_token",
    "render_help",
]
