"""Tool 端口：工具是纯数据对象（name/description/schema/execute）。

agent 循环只依赖本协议；具体工具在 builtin/ 下各自独立实现，
由组装根注册进 ToolRegistry。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from ..safety.sandbox import ExecutionSandbox


@dataclass
class ToolResult:
    """工具执行结果。is_error=True 时 content 是错误描述，
    仍会回填给 LLM（错误恢复原则：让模型自我修正而非中断循环）。"""

    content: str
    is_error: bool = False
    data: Dict[str, Any] = field(default_factory=dict)  # 给 UI/测试用的结构化附加信息


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", ToolSource(self.source))

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
        )
