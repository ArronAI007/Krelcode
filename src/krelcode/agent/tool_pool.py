"""单轮动态工具池：固定本轮可发现、可加载、可执行的工具边界。

Registry 是进程内能力目录；ToolRegistry 的快照先经过权限模式、运行模式、
功能开关、MCP 健康状态、用户禁用列表和 TurnIntent 过滤，再由本池决定暴露
层级。Provider schema 与执行查询共用同一个池，避免“只隐藏 schema，却仍能
执行”的边界漏洞。
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Dict, List, Optional, Sequence, Tuple

from ..tools.base import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    Tool,
    ToolCatalogMatch,
    ToolDescriptor,
    ToolSource,
    ToolTier,
)
from ..tools.registry import ToolRegistry
from .intent import TurnIntent

_TOOL_SEARCH_NAME = "tool_search"


@dataclass(frozen=True)
class ToolPoolEntry:
    name: str
    tool: Tool
    descriptor: ToolDescriptor


class TurnToolPool:
    """一轮查询内固定的候选目录，以及逐步扩展的已加载工具集合。

    候选集合在 turn 开始时一次确定，后续 ToolSearch 只能在集合内加载，不能
    绕过任何运行时过滤。执行还要求工具出现在最近一次 provider 请求中，因此
    ToolSearch 刚加载的工具不会在同一模型 Step 被并发执行。
    """

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

        self._loaded = frozenset(
            name for name, entry in self._entries.items()
            if entry.descriptor.tier is ToolTier.CORE
        )
        self._advertised = frozenset()

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
            and name != _TOOL_SEARCH_NAME
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
        """Return sorted full schemas and record this request's execute boundary."""
        names = tuple(sorted(self._loaded))
        self._advertised = frozenset(names)
        return [
            {
                "name": self._entries[name].tool.name,
                "description": self._entries[name].tool.description,
                "parameters": self._entries[name].tool.parameters,
            }
            for name in names
        ]

    def get(self, name: str) -> Optional[Tool]:
        """Return an eligible loaded tool; execution additionally checks advertise."""
        entry = self._entries.get(name)
        if entry is None or name not in self._loaded:
            return None
        return entry.tool

    def descriptor(self, name: str) -> Optional[ToolDescriptor]:
        entry = self._entries.get(name)
        return entry.descriptor if entry is not None else None

    def entry(self, name: str) -> Optional[ToolPoolEntry]:
        return self._entries.get(name)

    def catalog_entries(self) -> Tuple[ToolPoolEntry, ...]:
        """All tools that passed runtime filters, sorted by stable name."""
        return tuple(self._entries[name] for name in sorted(self._entries))

    def names(self) -> List[str]:
        """All eligible catalogue names, regardless of loaded state."""
        return sorted(self._entries)

    def loaded_names(self) -> List[str]:
        return sorted(self._loaded)

    def advertised_names(self) -> List[str]:
        return sorted(self._advertised)

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded

    def is_executable(self, name: str) -> bool:
        """Only tools advertised in the latest model request may execute."""
        return name in self._advertised and name in self._loaded

    def authorize_direct(self, name: str) -> bool:
        """Explicitly authorize one user-invoked direct tool for this turn."""
        if name not in self._entries:
            return False
        self._loaded = frozenset((*self._loaded, name))
        self._advertised = frozenset((*self._advertised, name))
        return True

    def unavailable_reason(self, name: str) -> Optional[str]:
        excluded = self._excluded.get(name)
        if excluded is not None:
            return excluded
        if name not in self._entries:
            return None
        if name not in self._loaded:
            return "工具尚未加载；请先调用 tool_search 并设置 load=true"
        if name not in self._advertised:
            return "工具刚在本轮加载，完整 schema 只从下一次模型请求开始可用"
        return None

    def excluded_reason(self, name: str) -> Optional[str]:
        return self._excluded.get(name)

    def excluded_items(self) -> Tuple[Tuple[str, str], ...]:
        return tuple((name, self._excluded[name]) for name in sorted(self._excluded))

    def search_catalog(
        self, query: str, *, limit: int = 5,
    ) -> Tuple[ToolCatalogMatch, ...]:
        """Search only the already-filtered eligible catalogue.

        Results are deterministic: match score descending, then tool name.
        Disabled, offline, run-mode-incompatible and permission-incompatible
        tools were removed before this method can observe them.
        """
        normalized = " ".join(str(query or "").casefold().split())
        if not normalized:
            return ()
        bounded = max(1, min(int(limit), 10))
        tokens = tuple(dict.fromkeys(normalized.replace("_", " ").split()))
        scored = []
        for name, entry in self._entries.items():
            score = _catalog_score(normalized, tokens, entry)
            if score <= 0:
                continue
            description = _one_line(entry.tool.description or "")
            scored.append((score, name, ToolCatalogMatch(
                name=name,
                description=description,
                tier=entry.descriptor.tier,
                source=entry.descriptor.source.value,
                read_only=entry.descriptor.read_only,
                loaded=name in self._loaded,
            )))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in scored[:bounded])

    def search(
        self, query: str, *, limit: int = 5,
    ) -> Tuple[ToolCatalogMatch, ...]:
        """Compatibility alias for the narrow ToolCatalog port."""
        return self.search_catalog(query, limit=limit)

    def load_tools(self, names: Sequence[str]) -> Tuple[str, ...]:
        """Load eligible tools by exact name; unknown/filtered names are ignored."""
        lookup = {name.casefold(): name for name in self._entries}
        requested = {
            lookup[str(raw).strip().casefold()]
            for raw in names
            if str(raw).strip().casefold() in lookup
        }
        if requested:
            self._loaded = frozenset((*self._loaded, *requested))
        return tuple(sorted(requested))

    def load(self, names: Sequence[str]) -> Tuple[str, ...]:
        """Compatibility alias for ToolSearch and diagnostics callers."""
        return self.load_tools(names)

    def system_hint(self, *, description_limit: int = 180) -> str:
        """Render the stable three-tier catalogue without full schemas."""
        if not self._entries:
            return ""

        core = [
            entry for entry in self.catalog_entries()
            if entry.descriptor.tier is ToolTier.CORE
        ]
        discoverable = [
            entry for entry in self.catalog_entries()
            if entry.descriptor.tier is ToolTier.DISCOVERABLE
        ]
        deferred_count = sum(
            entry.descriptor.tier is ToolTier.DEFERRED
            for entry in self._entries.values()
        )
        loaded_extra = [
            entry for entry in self.catalog_entries()
            if entry.descriptor.tier is not ToolTier.CORE
            and entry.name in self._loaded
        ]

        lines = [
            "<available_tools>",
            "Tools enabled for this turn. `read-only` calls may run in parallel; "
            "`mutating` calls run one at a time in model order.",
            "Core tools already have full schemas in the provider tools list:",
        ]
        for entry in core:
            category = "read-only" if entry.descriptor.read_only else "mutating"
            lines.append(f"- {escape(entry.name)} [{category}]")

        if discoverable:
            lines.append(
                "Discoverable tools expose metadata only. Use `tool_search` with "
                "`load=true` before calling one:")
            for entry in discoverable:
                description = _one_line(
                    entry.tool.description or "", description_limit)
                category = "read-only" if entry.descriptor.read_only else "mutating"
                lines.append(
                    f"- {escape(entry.name)} [{category}]: {escape(description)}")

        if deferred_count:
            lines.append(
                f"Deferred tools: {deferred_count}. They are searchable through "
                "`tool_search`; their schemas are loaded only on demand.")

        if loaded_extra:
            lines.append("Loaded this turn; full schemas are available now:")
            for entry in loaded_extra:
                lines.append(f"- {escape(entry.name)}")

        if (discoverable or deferred_count) and "tool_search" in self._entries:
            lines.append(
                "Call `tool_search(query=..., load=true)` to fetch matching tool "
                "schemas. A newly loaded tool becomes callable on the next model step.")
        lines.append("</available_tools>")
        return "\n".join(lines)


def _catalog_score(
    normalized_query: str,
    tokens: Tuple[str, ...],
    entry: ToolPoolEntry,
) -> int:
    name = entry.name.casefold()
    name_words = name.replace("_", " ").replace("-", " ")
    description = _one_line(entry.tool.description or "").casefold()
    terms = tuple(entry.descriptor.search_terms)

    score = 0
    if normalized_query in {name, name_words}:
        score += 1_000
    elif normalized_query in name_words:
        score += 480
    elif normalized_query in description:
        score += 160

    if "*" in tokens:
        return max(score, 1)

    for token in tokens:
        token_score = 0
        if token == name:
            token_score = 400
        elif token in name_words:
            token_score = 260
        elif any(token == term for term in terms):
            token_score = 220
        elif any(token in term for term in terms):
            token_score = 140
        elif token in description:
            token_score = 80
        if token_score == 0:
            return 0
        score += token_score
    return score


def _one_line(text: str, limit: int = 180) -> str:
    compact = " ".join((text or "").split())
    if len(compact) > limit:
        return compact[:limit].rstrip() + "…"
    return compact


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
