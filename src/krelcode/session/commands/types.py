"""Slash command 的纯数据契约。

命令元数据与执行器分离：registry 只负责发现、解析、别名和可用性过滤，
业务 handler 与 UI handler 仍由各自层提供。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


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
class CommandRequest:
    """完成 registry 解析后的单次命令调用。"""

    name: str
    arguments: str = ""
    raw_text: str = ""

    def split(self, maxsplit: int = -1) -> list[str]:
        return self.arguments.split(maxsplit=maxsplit)


@dataclass(frozen=True)
class CommandResult:
    """命令层的可渲染结果；不直接操作终端。"""

    handled: bool
    output: tuple[str, ...] = ()
    run_prompt: Optional[str] = None
    clear_tool_outputs: bool = False

    @classmethod
    def unhandled(cls) -> "CommandResult":
        return cls(handled=False)


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
