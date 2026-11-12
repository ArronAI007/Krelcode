"""Tool 端口：工具是纯数据对象（name/description/schema/execute）。

agent 循环只依赖本协议；具体工具在 builtin/ 下各自独立实现，
由组装根注册进 ToolRegistry。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Protocol


@dataclass
class ToolResult:
    """工具执行结果。is_error=True 时 content 是错误描述，
    仍会回填给 LLM（错误恢复原则：让模型自我修正而非中断循环）。"""

    content: str
    is_error: bool = False
    data: Dict[str, Any] = field(default_factory=dict)  # 给 UI/测试用的结构化附加信息


@dataclass
class ToolContext:
    """工具执行环境。Step 6 会在这里挂沙箱与审批回调。"""

    workspace_root: Path
    # 预留：approval_callback / logger / config


class Tool(Protocol):
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema (object)

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...
