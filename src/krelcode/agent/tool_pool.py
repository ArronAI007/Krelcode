"""单轮工具池：为一次 Agent turn 固化真正可暴露、可执行的工具集合。

Registry 是进程内能力目录；TurnToolPool 是某一轮的授权快照。provider schema
和工具执行查询必须共用同一个 pool，避免“只隐藏 schema，却仍能执行”的边界漏洞。
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Dict, List, Optional

from ..tools.base import Tool, ToolDescriptor
from ..tools.registry import ToolRegistry
from .intent import TurnIntent


@dataclass(frozen=True)
class ToolPoolEntry:
    name: str
    tool: Tool
    descriptor: ToolDescriptor


class TurnToolPool:
    """不可变的单轮工具快照。"""

    def __init__(
        self,
        registry: ToolRegistry,
        intent: Optional[TurnIntent] = None,
    ) -> None:
        self._entries: Dict[str, ToolPoolEntry] = {}
        self._excluded: Dict[str, str] = {}
        for tool, descriptor in registry.entries():
            if intent is not None and not intent.allows_tool(tool.name):
                self._excluded[tool.name] = "本轮请求意图限制了可用工具"
                continue
            self._entries[tool.name] = ToolPoolEntry(
                name=tool.name, tool=tool, descriptor=descriptor)

    def tool_specs(self) -> List[dict]:
        return [
            {
                "name": entry.tool.name,
                "description": entry.tool.description,
                "parameters": entry.tool.parameters,
            }
            for entry in self._entries.values()
        ]

    def get(self, name: str) -> Optional[Tool]:
        entry = self._entries.get(name)
        return entry.tool if entry is not None else None

    def descriptor(self, name: str) -> Optional[ToolDescriptor]:
        entry = self._entries.get(name)
        return entry.descriptor if entry is not None else None

    def names(self) -> List[str]:
        return sorted(self._entries)

    def excluded_reason(self, name: str) -> Optional[str]:
        return self._excluded.get(name)

    def system_hint(self, *, description_limit: int = 180) -> str:
        """生成当前轮工具目录，注入临时 system context。

        完整参数 schema 仍由 provider ``tools`` 参数传递；这里只补充高层目录，
        让模型明确本轮哪些能力可用，以及哪些调用具有写副作用。
        """
        if not self._entries:
            return ""
        lines = [
            "<available_tools>",
            "Tools enabled for this turn. `read-only` calls may run in parallel; "
            "`mutating` calls run one at a time in model order.",
        ]
        for entry in self._entries.values():
            description = " ".join((entry.tool.description or "").split())
            if len(description) > description_limit:
                description = description[:description_limit].rstrip() + "…"
            category = "read-only" if entry.descriptor.read_only else "mutating"
            lines.append(
                f"- {escape(entry.name)} [{category}]: {escape(description)}")
        lines.append("</available_tools>")
        return "\n".join(lines)
