"""Tool 端口：工具是纯数据对象（name/description/schema/execute）。

agent 循环只依赖本协议；具体工具在 builtin/ 下各自独立实现，
由组装根注册进 ToolRegistry。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Protocol, Tuple

from ..safety.sandbox import ExecutionSandbox


@dataclass
class ToolResult:
    """工具执行结果。is_error=True 时 content 是错误描述，
    仍会回填给 LLM（错误恢复原则：让模型自我修正而非中断循环）。"""

    content: str
    is_error: bool = False
    data: Dict[str, Any] = field(default_factory=dict)  # 给 UI/测试用的结构化附加信息


class RunMode(str, Enum):
    """当前进程发起查询的运行形态；工具池和权限据此做差异化暴露。"""

    REPL = "repl"
    PRINT = "print"
    JSON = "json"
    SDK = "sdk"
    REVIEW = "review"
    SUBAGENT = "subagent"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    READ_ONLY = "read-only"
    AUTO_APPROVE = "auto-approve"
    PLAN = "plan"


@dataclass(frozen=True)
class CommandDescriptor:
    """ToolUseContext 中可见的只读命令摘要，不暴露可执行命令对象。"""

    name: str
    description: str = ""
    kind: str = "command"
    source: str = "builtin"


@dataclass(frozen=True)
class ToolServices:
    """提供给工具/审批层的窄服务索引，避免传递整个应用容器。"""

    commands: Tuple[CommandDescriptor, ...] = ()
    mcp_servers: Tuple[str, ...] = ()

    def has_command(self, name: str) -> bool:
        normalized = name if name.startswith("/") else f"/{name}"
        return any(command.name == normalized for command in self.commands)


@dataclass(frozen=True)
class RuntimeSnapshot:
    """一轮查询的不可变运行时快照（AppState 的窄化替代）。"""

    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    run_mode: RunMode = RunMode.REPL
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    features: FrozenSet[str] = frozenset()
    disabled_tools: FrozenSet[str] = frozenset()
    alive_mcp_servers: FrozenSet[str] = frozenset()
    mcp_health_known: bool = False
    tool_allowlist: Optional[FrozenSet[str]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_mode", RunMode(self.run_mode))
        object.__setattr__(
            self, "permission_mode", PermissionMode(self.permission_mode))
        object.__setattr__(self, "features", frozenset(self.features or ()))
        object.__setattr__(
            self, "disabled_tools", frozenset(self.disabled_tools or ()))
        object.__setattr__(
            self, "alive_mcp_servers",
            frozenset(self.alive_mcp_servers or ()))
        if self.tool_allowlist is not None:
            object.__setattr__(
                self, "tool_allowlist", frozenset(self.tool_allowlist))


@dataclass
class TurnAbortSignal:
    """工具可见的协作式中止信号；Task.cancel 仍负责强制中断。"""

    _event: Any = field(default=None, repr=False)
    reason: str = ""

    def __post_init__(self) -> None:
        if self._event is None:
            self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return bool(self._event.is_set())

    def cancel(self, reason: str = "用户中止") -> None:
        self.reason = reason
        self._event.set()

    def check(self) -> None:
        if self.is_cancelled:
            raise asyncio.CancelledError(self.reason or "任务已中止")

    async def wait(self) -> None:
        await self._event.wait()


@dataclass
class ToolContext:
    """工具执行环境；默认自动创建运行时 Sandbox（fail-safe）。

    ``tool_descriptor`` 是 AgentLoop 为单次调用派生的上下文快照。工具和审批
    策略看到同一份来源/副作用信息，不共享可变的全局 AppState。
    """

    workspace_root: Path
    sandbox: Optional[ExecutionSandbox] = None
    sandbox_enabled: bool = True
    tool_descriptor: Optional["ToolDescriptor"] = None
    runtime: RuntimeSnapshot = field(default_factory=RuntimeSnapshot)
    services: ToolServices = field(default_factory=ToolServices)
    abort: TurnAbortSignal = field(default_factory=TurnAbortSignal)

    def __post_init__(self) -> None:
        if not self.sandbox_enabled:
            self.sandbox = None
        elif self.sandbox is None:
            self.sandbox = ExecutionSandbox(self.workspace_root)


class Tool(Protocol):
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema (object)

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...


class ToolSource(str, Enum):
    """工具来源；用于动态工具池过滤、/tools 展示和审计。"""

    BUILTIN = "builtin"
    SKILL = "skill"
    SUBAGENT = "subagent"
    MCP = "mcp"
    PLUGIN = "plugin"


@dataclass(frozen=True)
class ToolDescriptor:
    """Tool 的实现元数据，不进入 provider 的 function schema。"""

    source: ToolSource = ToolSource.BUILTIN
    mcp_server: Optional[str] = None
    read_only: bool = False
    required_feature: Optional[str] = None
    run_modes: Optional[FrozenSet[RunMode]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", ToolSource(self.source))
        if self.run_modes is not None:
            object.__setattr__(
                self,
                "run_modes",
                frozenset(RunMode(mode) for mode in self.run_modes),
            )

    @classmethod
    def from_tool(
        cls,
        tool: "Tool",
        *,
        source: ToolSource = ToolSource.BUILTIN,
        mcp_server: Optional[str] = None,
    ) -> "ToolDescriptor":
        """从 Tool 的可选能力标注构建描述符。

    未声明 ``read_only`` 的工具按 mutating 处理，这是调度器的保守默认值。
        """
        return cls(
            source=source,
            mcp_server=mcp_server,
            read_only=bool(getattr(tool, "read_only", False)),
            required_feature=getattr(tool, "required_feature", None),
            run_modes=getattr(tool, "run_modes", None),
        )
