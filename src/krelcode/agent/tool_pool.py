"""单轮工具池：为一次 Agent turn 固化真正可暴露、可执行的工具集合。

Registry 是进程内能力目录；TurnToolPool 是某一轮的授权快照。provider schema
和工具执行查询必须共用同一个 pool，避免“只隐藏 schema，却仍能执行”的边界漏洞。
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Dict, List, Optional

from ..tools.base import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    Tool,
    ToolDescriptor,
    ToolSource,
)
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
        runtime: Optional[RuntimeSnapshot] = None,
    ) -> None:
        self._entries: Dict[str, ToolPoolEntry] = {}
        self._excluded: Dict[str, str] = {}
        for tool, descriptor in registry.entries():
            exclusion = self._runtime_exclusion(tool.name, descriptor, runtime)
            if exclusion is not None:
                self._excluded[tool.name] = exclusion
                continue
            if intent is not None and not intent.allows_tool(tool.name):
                self._excluded[tool.name] = "本轮请求意图限制了可用工具"
                continue
            self._entries[tool.name] = ToolPoolEntry(
                name=tool.name, tool=tool, descriptor=descriptor)

    @staticmethod
    def _runtime_exclusion(
        name: str,
        descriptor: ToolDescriptor,
        runtime: Optional[RuntimeSnapshot],
    ) -> Optional[str]:
        if runtime is None:
            return None
        if name in runtime.disabled_tools:
            return "用户在配置中禁用了该工具"
        if (
            runtime.tool_allowlist is not None
            and name not in _allowed_names(runtime.tool_allowlist)
        ):
            return "当前 Skill 的 allowed-tools 不允许该工具"
        if (
            descriptor.required_feature is not None
            and descriptor.required_feature not in runtime.features
        ):
            return f"功能开关 {descriptor.required_feature} 未启用"
        if (
            descriptor.run_modes is not None
            and runtime.run_mode not in descriptor.run_modes
        ):
            return f"运行模式 {runtime.run_mode.value} 不暴露该工具"
        if (
            runtime.mcp_health_known
            and descriptor.source is ToolSource.MCP
            and descriptor.mcp_server not in runtime.alive_mcp_servers
        ):
            return f"MCP 服务器 {descriptor.mcp_server or '?'} 不在线"
        restricted = (
            runtime.permission_mode in {
                PermissionMode.READ_ONLY,
                PermissionMode.PLAN,
            }
            or runtime.run_mode is RunMode.REVIEW
        )
        # 远程 readOnlyHint 只用于调度展示，不能作为本地只读权限的凭证。
        security_read_only = (
            descriptor.read_only and descriptor.source is not ToolSource.MCP)
        if restricted and not security_read_only:
            return "当前权限/运行模式只允许本地只读工具"
        return None

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


_SKILL_TOOL_ALIASES = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "bash": "run_command",
    "glob": "glob",
    "grep": "grep",
    "webfetch": "fetch",
}


def _allowed_names(values) -> set[str]:
    allowed = set()
    for raw in values:
        name = str(raw).strip()
        if not name:
            continue
        allowed.add(name)
        allowed.add(_SKILL_TOOL_ALIASES.get(name.lower(), name))
    return allowed
