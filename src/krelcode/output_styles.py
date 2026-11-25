"""Declarative output-style discovery and prompt injection.

An output style is Markdown plus a small frontmatter block. It is *not* an
executable extension. Styles may be supplied by the user, the project, or an
explicit configuration path; project styles override user styles with the same
name, while explicit paths have the highest priority.

The active style is represented as one traceable PromptFragment. Switching or
reloading styles never mutates the base system prompt: the composition root
replaces only the ``style:output`` fragment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from .agent.prompt.fragments import (
    GROUP_SYSTEM,
    LAYER_L4_AGENT_STYLE,
    STABILITY_SESSION,
    PromptFragment,
    PromptFragmentRegistry,
    TRUST_MANAGED,
    TRUST_PROJECT,
    TRUST_USER,
)

MAX_STYLE_BYTES = 64 * 1024
MAX_STYLE_NAME = 80
DEFAULT_OUTPUT_STYLE_NAME = "default"
BUILTIN_OUTPUT_STYLE_SCOPE = "builtin"
_STYLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$")


@dataclass(frozen=True)
class OutputStyle:
    name: str
    content: str
    description: str = ""
    scope: str = "user"
    path: Optional[Path] = None
    priority: int = 100
    keep_coding_instructions: bool = False

    @property
    def builtin(self) -> bool:
        return self.scope == BUILTIN_OUTPUT_STYLE_SCOPE

    @property
    def source(self) -> str:
        location = str(self.path) if self.path is not None else ""
        return f"output-style:{self.scope}:{location}" if location else (
            f"output-style:{self.scope}:{self.name}")

    def usage(self) -> str:
        description = f"  {self.description}" if self.description else ""
        return f"{self.name:<18} [{self.scope}]{description}"


EXPLANATORY_OUTPUT_STYLE = OutputStyle(
    name="explanatory",
    description="解释关键决策、取舍与实现原理",
    scope=BUILTIN_OUTPUT_STYLE_SCOPE,
    priority=0,
    keep_coding_instructions=True,
    content="""\
# Explanatory

Make the user's understanding of the work a first-class outcome while keeping
the primary response action-oriented.

- After a meaningful implementation decision, behavior change, or debugging
  conclusion, add a short `Insight` note explaining why the chosen approach
  works, the important tradeoff, and any material alternative that was rejected.
- Prefer causal explanations and concrete repository evidence over generic
  textbook descriptions. Keep each insight concise and tied to the current task.
- Do not narrate trivial steps, repeat code that is already visible, or add an
  insight after every tool call. Skip the note when it would add no useful
  understanding.
- The final answer must still state what changed, how it was verified, and any
  remaining risk.
""",
)


LEARNING_OUTPUT_STYLE = OutputStyle(
    name="learning",
    description="通过协作与实践帮助用户理解并参与实现",
    scope=BUILTIN_OUTPUT_STYLE_SCOPE,
    priority=0,
    keep_coding_instructions=True,
    content="""\
# Learning

Treat the user as an active collaborator learning the codebase by doing.

- Explain the goal, the relevant evidence, and the reason for each major step as
  the task progresses. Invite questions when a concept is central to the change.
- When a small, reversible, non-critical step is a good learning exercise, leave
  a clearly marked `TODO(human)` with a focused instruction, explain the expected
  result, and wait for the user before continuing that step.
- Never delegate security-sensitive work, destructive operations, migrations,
  credential handling, or changes whose failure would block the task. If the user
  asks you to complete the work directly, do so without forcing a teaching detour.
- Keep the repository in a coherent state at every pause, and verify the final
  result rather than treating explanation as a substitute for execution.
""",
)


BUILTIN_OUTPUT_STYLES: tuple[OutputStyle, ...] = (
    OutputStyle(
        name=DEFAULT_OUTPUT_STYLE_NAME,
        description="Krelcode 默认编码人格，不额外注入输出风格 Prompt",
        scope=BUILTIN_OUTPUT_STYLE_SCOPE,
        priority=0,
        keep_coding_instructions=True,
        content="",
    ),
    EXPLANATORY_OUTPUT_STYLE,
    LEARNING_OUTPUT_STYLE,
)
_BUILTIN_STYLE_ORDER = {
    style.name: index for index, style in enumerate(BUILTIN_OUTPUT_STYLES)
}


@dataclass(frozen=True)
class OutputStyleIssue:
    path: Optional[Path]
    message: str


@dataclass
class OutputStyleConfig:
    enabled: bool = True
    active: str = ""
    paths: tuple[Path, ...] = ()
    home: Path = field(default_factory=Path.home)

    @classmethod
    def from_config(
        cls,
        section: Any,
        *,
        workspace: Optional[Path] = None,
        cli_paths: Sequence[str | Path] = (),
        cli_active: Optional[str] = None,
        home: Optional[Path] = None,
    ) -> "OutputStyleConfig":
        data = section if isinstance(section, Mapping) else {}
        configured = data.get("paths") or ()
        if isinstance(configured, (str, Path)):
            configured = (configured,)
        paths: list[Path] = []
        for raw in (*configured, *cli_paths):
            path = Path(raw).expanduser()
            if not path.is_absolute() and workspace is not None:
                path = Path(workspace) / path
            paths.append(path)
        active = (
            str(cli_active).strip() if cli_active is not None
            else str(data.get("active") or data.get("default") or "").strip()
        )
        return cls(
            enabled=bool(data.get("enabled", True)),
            active=active,
            paths=tuple(paths),
            home=Path(home).expanduser() if home is not None else Path.home(),
        )


class OutputStyleManager:
    """Discover, select and apply declarative Markdown output styles."""

    def __init__(
        self,
        workspace_root: str | Path,
        config: Optional[OutputStyleConfig] = None,
        *,
        home: Optional[Path] = None,
        extra_paths: Iterable[str | Path] = (),
        enabled: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.config = config or OutputStyleConfig(
            enabled=enabled,
            home=Path(home).expanduser() if home is not None else Path.home(),
            paths=tuple(Path(item).expanduser() for item in extra_paths),
        )
        self.enabled = bool(config.enabled) if config is not None else bool(enabled)
        self._styles: dict[str, OutputStyle] = {}
        self._conflicts: list[str] = []
        self.issues: list[OutputStyleIssue] = []
        self.active_name = DEFAULT_OUTPUT_STYLE_NAME
        self.reload()
        if self.config.active:
            self.select(self.config.active)

    @property
    def styles(self) -> tuple[OutputStyle, ...]:
        return tuple(sorted(self._styles.values(), key=self._style_sort_key))

    @property
    def active(self) -> Optional[OutputStyle]:
        return self._styles.get(self.active_name)

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(self._conflicts)

    def get(self, name: str) -> Optional[OutputStyle]:
        return self._styles.get(str(name).strip())

    def reload(self) -> "OutputStyleManager":
        self._styles.clear()
        self._conflicts.clear()
        self.issues.clear()
        if not self.enabled:
            return self
        for style in BUILTIN_OUTPUT_STYLES:
            self._styles[style.name] = style
        roots = [
            (self.config.home / ".krelcode" / "output-styles", "user", 100),
            (self.workspace_root / ".krelcode" / "output-styles", "project", 200),
        ]
        roots.extend((path, "configured", 300) for path in self.config.paths)
        seen_paths: set[Path] = set()
        candidates: list[OutputStyle] = []
        for root, scope, priority in roots:
            for path in self._iter_style_paths(root):
                resolved = path.resolve()
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)
                style = self._load_style(path, scope, priority)
                if style is not None:
                    candidates.append(style)
        # Higher scope wins; equal-priority styles are deterministic by path.
        candidates.sort(key=lambda item: (item.priority, str(item.path or "")))
        for style in candidates:
            existing = self._styles.get(style.name)
            if existing is not None:
                self._conflicts.append(
                    f"{style.name}: 保留 {existing.scope} {existing.path}，"
                    f"忽略 {style.scope} {style.path}")
            self._styles[style.name] = style
        if self.active_name and self.active_name not in self._styles:
            self.issues.append(OutputStyleIssue(
                None, f"当前 output style 不存在: {self.active_name}"))
            self.active_name = DEFAULT_OUTPUT_STYLE_NAME
        return self

    def select(self, name: str) -> Optional[OutputStyle]:
        normalized = str(name).strip()
        if normalized.lower() in {"", DEFAULT_OUTPUT_STYLE_NAME}:
            self.active_name = DEFAULT_OUTPUT_STYLE_NAME
            return self._styles.get(DEFAULT_OUTPUT_STYLE_NAME)
        if normalized.lower() in {"off", "none"}:
            self.active_name = ""
            return None
        style = self._styles.get(normalized)
        if style is None:
            return None
        self.active_name = style.name
        return style

    def apply_to_registry(
        self, registry: PromptFragmentRegistry,
    ) -> Optional[PromptFragment]:
        fragment = self.fragment()
        if fragment is None:
            registry.remove("style:output")
            return None
        if registry.get("style:output") is None:
            registry.register(fragment)
        else:
            registry.upsert(fragment)
        return fragment

    def fragment(self) -> Optional[PromptFragment]:
        style = self.active
        if style is None or not style.content.strip():
            return None
        coding_contract = (
            "This style is compatible with and additive to the L1 coding "
            "instructions. Keep those instructions in force unless this style "
            "provides a more specific response behavior."
            if style.keep_coding_instructions else
            "Within response strategy, explanation depth, and interaction pattern, "
            "this active style supersedes conflicting L1 coding-style guidance. "
            "L1 still governs whenever this style is silent."
        )
        content = (
            "<output_style "
            f'name="{_escape_attr(style.name)}" '
            f'scope="{_escape_attr(style.scope)}" '
            f'keep-coding-instructions="{str(style.keep_coding_instructions).lower()}">\n'
            f"{style.content.strip()}\n"
            "</output_style>\n"
            "Apply this output style to response behavior and presentation. "
            f"{coding_contract} It must never override managed/user/project "
            "instructions, safety, evidence requirements, tool permissions, "
            "sandbox rules, or workspace boundaries. Do not expose hidden chain-of-"
            "thought; provide concise rationale and observable evidence instead."
        )
        return PromptFragment(
            id="style:output",
            source=style.source,
            title=f"Output style: {style.name}",
            content=content,
            group=GROUP_SYSTEM,
            priority=10,
            cacheable=True,
            compactable=False,
            toggleable=False,
            metadata={
                "style": style.name,
                "scope": style.scope,
                "path": str(style.path) if style.path else "",
                "builtin": style.builtin,
                "keep_coding_instructions": style.keep_coding_instructions,
            },
            scope=style.scope,
            layer=LAYER_L4_AGENT_STYLE,
            stability=STABILITY_SESSION,
            trust=(
                TRUST_MANAGED if style.builtin else
                TRUST_USER if style.scope == "user" else TRUST_PROJECT
            ),
        )

    def _style_sort_key(self, style: OutputStyle) -> tuple[int, int, str]:
        if style.builtin:
            return (0, _BUILTIN_STYLE_ORDER.get(style.name, 999), style.name)
        return (1, style.priority, style.name)

    def status_lines(self) -> tuple[str, ...]:
        if not self.enabled:
            return ("  Output Styles 已禁用",)
        active = self.active_name or DEFAULT_OUTPUT_STYLE_NAME
        if active == DEFAULT_OUTPUT_STYLE_NAME:
            active += "（未注入额外样式）"
        lines = [f"  当前 Output Style: {active}"]
        if not self.styles:
            lines.append(
                "  （未发现样式；可创建 ~/.krelcode/output-styles/*.md "
                "或 .krelcode/output-styles/*.md）")
        else:
            lines.append("  可用样式:")
            lines.extend(f"  - {item.usage()}" for item in self.styles)
        if self._conflicts:
            lines.append("  覆盖关系:")
            lines.extend(f"  ! {item}" for item in self._conflicts)
        if self.issues:
            lines.extend(f"  ! {item.path or ''}: {item.message}" for item in self.issues)
        return tuple(lines)

    def _iter_style_paths(self, root: Path) -> Iterable[Path]:
        root = root.expanduser()
        if root.is_file():
            yield root
            return
        if not root.is_dir():
            return
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            self.issues.append(OutputStyleIssue(root, f"无法读取目录: {exc}"))
            return
        for child in children:
            if child.name.startswith(".") or child.suffix.lower() != ".md":
                continue
            if child.is_file():
                yield child

    def _load_style(
        self, path: Path, scope: str, priority: int,
    ) -> Optional[OutputStyle]:
        try:
            if path.stat().st_size > MAX_STYLE_BYTES:
                self.issues.append(OutputStyleIssue(
                    path, f"文件超过 {MAX_STYLE_BYTES} bytes，已跳过"))
                return None
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            self.issues.append(OutputStyleIssue(path, f"读取失败: {exc}"))
            return None
        metadata, body = _parse_frontmatter(text)
        name = str(metadata.get("name") or path.stem).strip()
        if not _STYLE_ID.match(name) or len(name) > MAX_STYLE_NAME:
            self.issues.append(OutputStyleIssue(
                path, f"非法 style name: {name!r}（只允许字母数字._-）"))
            return None
        if not body.strip():
            self.issues.append(OutputStyleIssue(path, "样式正文为空，已跳过"))
            return None
        keep_coding_instructions = _parse_optional_bool(
            metadata.get("keep-coding-instructions")
            if "keep-coding-instructions" in metadata
            else metadata.get("keep_coding_instructions"),
            default=False,
        )
        if keep_coding_instructions is None:
            self.issues.append(OutputStyleIssue(
                path,
                "keep-coding-instructions 必须是 true/false、yes/no、on/off 或 1/0；"
                "已按 false 处理",
            ))
            keep_coding_instructions = False
        return OutputStyle(
            name=name,
            content=body.strip() + "\n",
            description=str(metadata.get("description") or "").strip(),
            scope=scope,
            path=path.resolve(),
            priority=priority,
            keep_coding_instructions=keep_coding_instructions,
        )


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = index
            break
    if end is None:
        return {}, text
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        match = _FRONTMATTER_KEY.match(line.strip())
        if match:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            metadata[match.group(1).strip().lower()] = value
    return metadata, "".join(lines[end + 1:])


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace(
        "<", "&lt;").replace(">", "&gt;")


def _parse_optional_bool(value: object, *, default: bool) -> Optional[bool]:
    if value is None or str(value).strip() == "":
        return default
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    return None


__all__ = [
    "BUILTIN_OUTPUT_STYLES",
    "BUILTIN_OUTPUT_STYLE_SCOPE",
    "DEFAULT_OUTPUT_STYLE_NAME",
    "EXPLANATORY_OUTPUT_STYLE",
    "LEARNING_OUTPUT_STYLE",
    "OutputStyle",
    "OutputStyleConfig",
    "OutputStyleIssue",
    "OutputStyleManager",
]
