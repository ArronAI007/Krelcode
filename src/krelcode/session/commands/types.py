"""Slash command 的纯数据契约。

命令元数据与执行器分离：registry 只负责发现、解析、别名和可用性过滤，
业务 handler 与 UI handler 仍由各自层提供。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommandKind(str, Enum):
    COMMAND = "command"
    UI_COMMAND = "ui_command"
    CONTROL = "control"
    PROMPT = "prompt"


class CommandSource(str, Enum):
    BUILTIN = "builtin"
    SKILL = "skill"
    MCP = "mcp"


@dataclass(frozen=True)
class CommandSpec:
    """一个可解析 slash command；name/aliases 不包含前导斜杠。"""

    name: str
    description: str
    kind: CommandKind = CommandKind.COMMAND
    source: CommandSource = CommandSource.BUILTIN
    aliases: tuple[str, ...] = ()
    argument_hint: str = ""
    help: str = ""
    is_enabled: bool = True
    is_hidden: bool = False
    available_during_run: bool = False

    @property
    def invocation(self) -> str:
        return f"/{self.name}"

    @property
    def usage(self) -> str:
        suffix = f" {self.argument_hint}" if self.argument_hint else ""
        return f"{self.invocation}{suffix}"

    @property
    def help_text(self) -> str:
        return self.help or self.description
