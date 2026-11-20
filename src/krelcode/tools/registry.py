"""工具注册表：管理执行实例、运行时描述符与统一 ToolPromptSpec。"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .base import Tool, ToolDescriptor
from .prompt import ToolPromptSpec


class ToolRegistry:
    def __init__(self, tools: Optional[List[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        self._descriptors: Dict[str, ToolDescriptor] = {}
        self._prompt_specs: Dict[str, ToolPromptSpec] = {}
        for tool in tools or []:
            self.register(tool)

    def register(
        self,
        tool: Tool,
        descriptor: Optional[ToolDescriptor] = None,
        prompt_spec: Optional[ToolPromptSpec] = None,
    ) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        resolved_descriptor = descriptor or ToolDescriptor.from_tool(tool)
        resolved_prompt = prompt_spec or ToolPromptSpec.from_tool(
            tool,
            source=resolved_descriptor.source.value,
            # Registry-compatible tools historically allowed an empty
            # description. Keep those tools registrable while ensuring the
            # canonical provider spec still has a deterministic model-facing
            # description.
            fallback_description=f"Tool {tool.name}",
        )
        if resolved_prompt.name != tool.name:
            raise ValueError(
                f"ToolPromptSpec {resolved_prompt.name!r} 与 Tool.name {tool.name!r} 不一致")
        self._tools[tool.name] = tool
        self._descriptors[tool.name] = resolved_descriptor
        self._prompt_specs[tool.name] = resolved_prompt

    def entries(self) -> List[Tuple[Tool, ToolDescriptor]]:
        """返回按名称稳定排序的工具/描述符快照，供单轮工具池构建。"""
        return [
            (tool, self._descriptors[name])
            for name, tool in sorted(self._tools.items())
        ]

    def descriptor(self, name: str) -> Optional[ToolDescriptor]:
        return self._descriptors.get(name)

    def prompt_spec(self, name: str) -> Optional[ToolPromptSpec]:
        return self._prompt_specs.get(name)

    def prompt_entries(self) -> List[Tuple[Tool, ToolDescriptor, ToolPromptSpec]]:
        """Return sorted execution/runtime/model-facing contracts together."""
        return [
            (tool, self._descriptors[name], self._prompt_specs[name])
            for name, tool in sorted(self._tools.items())
        ]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def unregister(self, name: str) -> bool:
        """移除一个工具；返回是否存在过。"""
        self._descriptors.pop(name, None)
        self._prompt_specs.pop(name, None)
        return self._tools.pop(name, None) is not None

    def unregister_prefix(self, prefix: str) -> int:
        """移除所有以 prefix 开头的工具（MCP 服务器下线/热重启用）；返回移除数。"""
        names = [n for n in self._tools if n.startswith(prefix)]
        for n in names:
            del self._tools[n]
            self._descriptors.pop(n, None)
            self._prompt_specs.pop(n, None)
        return len(names)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def tool_specs(self) -> List[dict]:
        """生成统一 ToolSpec（{"name","description","parameters"}）。

        各 provider adapter 负责把它转成自己的 wire 格式
        （OpenAI: function.parameters / Anthropic: input_schema）。
        """
        return [
            self._prompt_specs[name].as_tool_spec()
            for name in sorted(self._tools)
        ]
