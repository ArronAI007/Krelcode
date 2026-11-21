"""内置与用户自定义 Agent Definition 的发现与校验。

内置 Claude Code 兼容 agents（Explore、Plan、general-purpose、claude、
statusline-setup、claude-code-guide）与 Krelcode 的独立 ``verifier``；用户定义
仍可覆盖同名内置定义。
定义文件是带 frontmatter 的 Markdown：
``~/.krelcode/agents/reviewer.md`` 或
``<workspace>/.krelcode/agents/reviewer.md``。项目级同名定义覆盖全局。
Definition 只能缩小工具集合，不能绕过审批、Sandbox 或子代理防递归限制。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from ..safety.runtime import parse_permission_mode
from ..skills.parser import SkillParseError, parse_skill_document, split_allowed_tools

MAX_AGENT_DEFINITION_BYTES = 256_000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    system_prompt: str
    allowed_tools: Tuple[str, ...] = ()
    source: str = ""
    path: Optional[Path] = None
    default_isolated: bool = False
    default_fork_context: bool = False
    model: str = ""
    permission_mode: str = ""
    mcp_servers: Optional[Tuple[str, ...]] = None
    inherit_project_rules: bool = True

    def system_prompt_with_base(self, base: str) -> str:
        tools = ", ".join(self.allowed_tools) if self.allowed_tools else "（继承默认工具池）"
        runtime = []
        if self.model:
            runtime.append(f"Model: {self.model}")
        if self.permission_mode:
            runtime.append(f"Permission mode: {self.permission_mode}")
        if self.mcp_servers is not None:
            mcp = ", ".join(self.mcp_servers) if self.mcp_servers else "（无）"
            runtime.append(f"MCP servers: {mcp}")
        if not self.inherit_project_rules:
            runtime.append("Project/user instruction files: omitted")
        runtime_text = ("\n" + "\n".join(runtime)) if runtime else ""
        return (
            f"{base.rstrip()}\n\n"
            f'<agent_definition name="{self.name}">\n'
            f"Description: {self.description}\n"
            f"Allowed tools: {tools}{runtime_text}\n\n"
            f"{self.system_prompt.strip()}\n"
            "</agent_definition>"
        )


@dataclass(frozen=True)
class AgentDefinitionIssue:
    path: Path
    message: str


def builtin_agent_definitions() -> Tuple[AgentDefinition, ...]:
    """Return trusted built-ins that user/project files may override."""
    from ..agent.verification import (
        VERIFICATION_AGENT_ALLOWED_TOOLS,
        VERIFICATION_AGENT_DESCRIPTION,
        VERIFICATION_AGENT_NAME,
        VERIFICATION_AGENT_SYSTEM_PROMPT,
    )
    from .builtin_agents import CLAUDE_BUILTIN_AGENT_SPECS

    definitions = [
        AgentDefinition(
            name=spec.name,
            description=spec.description,
            system_prompt=spec.system_prompt,
            allowed_tools=spec.allowed_tools,
            source="builtin",
            model=spec.model,
            permission_mode=spec.permission_mode,
            mcp_servers=spec.mcp_servers,
            inherit_project_rules=spec.inherit_project_rules,
        )
        for spec in CLAUDE_BUILTIN_AGENT_SPECS
    ]
    definitions.append(AgentDefinition(
        name=VERIFICATION_AGENT_NAME,
        description=VERIFICATION_AGENT_DESCRIPTION,
        system_prompt=VERIFICATION_AGENT_SYSTEM_PROMPT,
        allowed_tools=VERIFICATION_AGENT_ALLOWED_TOOLS,
        source="builtin",
        mcp_servers=(),
    ))
    return tuple(definitions)


def _frontmatter_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class _Root:
    path: Path
    source: str


class AgentDefinitionManager:
    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        extra_paths: Optional[Iterable[str | Path]] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = (Path(home).expanduser() if home is not None else Path.home()).expanduser().resolve()
        self.extra_paths = tuple(Path(p).expanduser() for p in (extra_paths or ()))
        self._definitions: Dict[str, AgentDefinition] = {}
        self._issues: Tuple[AgentDefinitionIssue, ...] = ()
        self._roots = self._build_roots()
        self.reload()

    def _build_roots(self) -> Tuple[_Root, ...]:
        candidates = [
            _Root(self.home / ".krelcode" / "agents", "global"),
            _Root(self.workspace_root / ".krelcode" / "agents", "project"),
            _Root(self.workspace_root / ".agents" / "agents", "project"),
        ]
        for raw in self.extra_paths:
            path = raw if raw.is_absolute() else self.workspace_root / raw
            candidates.append(_Root(path.resolve(), "custom"))
        seen: set[Path] = set()
        roots = []
        for root in candidates:
            resolved = root.path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            roots.append(_Root(resolved, root.source))
        return tuple(roots)

    def set_extra_paths(
        self, paths: Iterable[str | Path], *, reload: bool = True,
    ) -> None:
        """Replace plugin-provided Agent Definition roots."""
        self.extra_paths = tuple(Path(p).expanduser() for p in paths)
        self._roots = self._build_roots()
        if reload:
            self.reload()

    def reload(self) -> None:
        found: Dict[str, AgentDefinition] = {
            definition.name: definition
            for definition in builtin_agent_definitions()
        }
        issues = []
        for root in self._roots:
            if not root.path.is_dir():
                continue
            try:
                paths = sorted(root.path.glob("*.md"), key=lambda p: p.name.lower())
            except OSError as exc:
                issues.append(AgentDefinitionIssue(root.path, f"无法扫描目录: {exc}"))
                continue
            for path in paths:
                try:
                    definition = self._load(path, root)
                except (OSError, ValueError, SkillParseError) as exc:
                    issues.append(AgentDefinitionIssue(path, str(exc)))
                    continue
                found[definition.name] = definition
        self._definitions = found
        self._issues = tuple(issues)

    def _load(self, path: Path, root: _Root) -> AgentDefinition:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.path):
            raise ValueError("Agent definition 越出定义目录")
        size = resolved.stat().st_size
        if size > MAX_AGENT_DEFINITION_BYTES:
            raise ValueError(
                f"Agent definition 过大（{size} > {MAX_AGENT_DEFINITION_BYTES} bytes）")
        text = resolved.read_text(encoding="utf-8")
        metadata, body = parse_skill_document(text, fallback_name=resolved.stem)
        name = str(metadata.get("name") or resolved.stem).strip()
        if not NAME_RE.match(name):
            raise ValueError(f"非法 Agent Definition 名称: {name!r}")
        description = " ".join(str(metadata.get("description") or "").split())
        if not description:
            raise ValueError("frontmatter 缺少非空 description")
        prompt = body.strip()
        if not prompt:
            raise ValueError("Agent Definition 正文（system prompt）为空")
        tools = split_allowed_tools(
            metadata.get("allowed-tools", metadata.get("tools")))
        raw_permission = metadata.get(
            "permission-mode", metadata.get("permission_mode", ""))
        permission_mode = ""
        if raw_permission not in (None, ""):
            try:
                permission_mode = parse_permission_mode(
                    str(raw_permission)).value
            except ValueError as exc:
                raise ValueError(
                    f"非法 permission-mode: {raw_permission!r}") from exc
        raw_mcp = metadata.get("mcp-servers", metadata.get("mcp_servers"))
        if raw_mcp is None:
            mcp_servers = None
        elif str(raw_mcp).strip() == "*":
            mcp_servers = ("*",)
        else:
            mcp_servers = split_allowed_tools(raw_mcp)
        inherit_project_rules = _frontmatter_bool(
            metadata.get(
                "inherit-project-rules",
                metadata.get("inherit_project_rules")),
            default=True,
        )
        omit_project_rules = _frontmatter_bool(
            metadata.get(
                "omit-project-rules",
                metadata.get("omit_project_rules")),
            default=False,
        )
        if omit_project_rules:
            inherit_project_rules = False
        return AgentDefinition(
            name=name,
            description=description,
            system_prompt=prompt,
            allowed_tools=tools,
            source=root.source,
            path=resolved,
            default_isolated=bool(metadata.get("isolated", False)),
            default_fork_context=bool(
                metadata.get("fork-context", metadata.get("fork_context", False))),
            model=str(metadata.get("model") or "").strip(),
            permission_mode=permission_mode,
            mcp_servers=mcp_servers,
            inherit_project_rules=inherit_project_rules,
        )

    def get(self, name: str) -> AgentDefinition:
        key = str(name or "").strip()
        definition = self._definitions.get(key)
        if definition is None:
            raise KeyError(f"未知 Agent Definition: {key or '（空）'}")
        return definition

    def list(self) -> Tuple[AgentDefinition, ...]:
        return tuple(sorted(self._definitions.values(), key=lambda d: d.name))

    @property
    def issues(self) -> Tuple[AgentDefinitionIssue, ...]:
        return self._issues

    def status_lines(self) -> Tuple[str, ...]:
        lines = []
        for definition in self.list():
            tools = ", ".join(definition.allowed_tools) or "默认"
            runtime = []
            if definition.model:
                runtime.append(f"model={definition.model}")
            if definition.permission_mode:
                runtime.append(f"permission={definition.permission_mode}")
            if definition.mcp_servers is not None:
                mcp = ",".join(definition.mcp_servers) or "none"
                runtime.append(f"mcp={mcp}")
            if not definition.inherit_project_rules:
                runtime.append("project-rules=omitted")
            runtime_text = " · " + " · ".join(runtime) if runtime else ""
            location = str(definition.path) if definition.path else "built-in"
            lines.append(
                f"{definition.name} [{definition.source}] {definition.description} "
                f"· tools: {tools}{runtime_text} · {location}")
        return tuple(lines)
