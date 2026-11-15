"""工具注册表：管理 Tool 实例，并生成发给 LLM 的 OpenAI tools schema。"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .base import Tool, ToolDescriptor


class ToolRegistry:
    def __init__(self, tools: Optional[List[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        self._descriptors: Dict[str, ToolDescriptor] = {}
        for tool in tools or []:
            self.register(tool)

    def register(
        self,
        tool: Tool,
        descriptor: Optional[ToolDescriptor] = None,
    ) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool
        self._descriptors[tool.name] = descriptor or ToolDescriptor.from_tool(tool)

    def entries(self) -> List[Tuple[Tool, ToolDescriptor]]:
        """返回一致的工具/描述符快照，供单轮工具池构建。"""
        return [
            (tool, self._descriptors[name])
            for name, tool in self._tools.items()
        ]

    def descriptor(self, name: str) -> Optional[ToolDescriptor]:
        return self._descriptors.get(name)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def unregister(self, name: str) -> bool:
        """移除一个工具；返回是否存在过。"""
        self._descriptors.pop(name, None)
        return self._tools.pop(name, None) is not None

    def unregister_prefix(self, prefix: str) -> int:
        """移除所有以 prefix 开头的工具（MCP 服务器下线/热重启用）；返回移除数。"""
        names = [n for n in self._tools if n.startswith(prefix)]
        for n in names:
            del self._tools[n]
            self._descriptors.pop(n, None)
        return len(names)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def tool_specs(self) -> List[dict]:
        """生成统一 ToolSpec（{"name","description","parameters"}）。

        各 provider adapter 负责把它转成自己的 wire 格式
        （OpenAI: function.parameters / Anthropic: input_schema）。
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]
