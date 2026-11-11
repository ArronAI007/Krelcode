"""工具注册表：管理 Tool 实例，并生成发给 LLM 的 OpenAI tools schema。"""

from __future__ import annotations

from typing import Dict, List, Optional

from .base import Tool


class ToolRegistry:
    def __init__(self, tools: Optional[List[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

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
