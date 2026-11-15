"""工作区上下文：分层收集、优先级装配与 token 预算。

默认来源（全部失败安全）：
1. 全局 + 项目规则：``~/.krelcode/AGENTS.md``、``AGENTS.md``/``KRELCODE.md``
2. git 快照：当前分支 + porcelain 状态
3. 目录树：剪枝隐藏/依赖目录，限深度与条目数

对外保留 ``build_workspace_context() -> str`` 兼容接口；新代码可消费
``WorkspaceContext`` 获取来源、估算 token 和逐段预算结果。
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..tools.fsutils import is_hidden_file, is_pruned_dir, truncate_middle
from .tokens import estimate_text_tokens

TREE_MAX_DEPTH = 3
TREE_MAX_ENTRIES = 150
AGENTS_MD_MAX_CHARS = 8_000
GIT_STATUS_MAX_CHARS = 2_000
DEFAULT_CONTEXT_MAX_TOKENS = 6_000
MIN_SECTION_TOKENS = 24
TRUNCATION_MARKER = "\n…[上下文已按 token 预算截断]"
BUNDLE_OVERHEAD_TOKENS = estimate_text_tokens(
    "<workspace_context>\n\n</workspace_context>")
AGENTS_MD_NAMES = ("AGENTS.md", "KRELCODE.md")
GLOBAL_RULES_RELPATH = Path(".krelcode") / "AGENTS.md"   # ~/.krelcode/AGENTS.md


@dataclass(frozen=True)
class ContextOptions:
    max_tokens: int = DEFAULT_CONTEXT_MAX_TOKENS
    include_rules: bool = True
    include_git: bool = True
    include_tree: bool = True
    tree_max_depth: int = TREE_MAX_DEPTH
    tree_max_entries: int = TREE_MAX_ENTRIES
    rules_max_chars: int = AGENTS_MD_MAX_CHARS

    @classmethod
    def from_config(cls, value: Optional[Dict[str, Any]]) -> "ContextOptions":
        if not isinstance(value, dict):
            return cls()

        def positive_int(key: str, default: int, minimum: int = 1,
                         maximum: int = 1_000_000) -> int:
            try:
                parsed = int(value.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(minimum, min(parsed, maximum))

        return cls(
            max_tokens=positive_int(
                "max_tokens", DEFAULT_CONTEXT_MAX_TOKENS, 256, 100_000),
            include_rules=bool(value.get("include_rules", True)),
            include_git=bool(value.get("include_git", True)),
            include_tree=bool(value.get("include_tree", True)),
            tree_max_depth=positive_int("tree_max_depth", TREE_MAX_DEPTH, 1, 12),
            tree_max_entries=positive_int(
                "tree_max_entries", TREE_MAX_ENTRIES, 10, 5_000),
            rules_max_chars=positive_int(
                "rules_max_chars", AGENTS_MD_MAX_CHARS, 256, 100_000),
        )


@dataclass(frozen=True)
class ContextSection:
    name: str
    content: str
    priority: int = 50
    source: str = ""

    @property
    def tokens(self) -> int:
        return estimate_text_tokens(self.content)


@dataclass(frozen=True)
class WorkspaceContext:
    root: Path
    sections: tuple[ContextSection, ...]
    text: str
    max_tokens: int
    total_tokens: int
    omitted: tuple[str, ...] = ()

    def summary_lines(self) -> List[str]:
        lines = [
            f"预算 {self.total_tokens}/{self.max_tokens} tokens"
            f" · 来源 {len(self.sections)} 个"
        ]
        for section in self.sections:
            lines.append(f"{section.name}: {section.tokens} tokens")
        if self.omitted:
            lines.append("未注入: " + ", ".join(self.omitted))
        return lines


# ---------------------------------------------------------------------------
# 目录树
# ---------------------------------------------------------------------------

def render_tree(
    root: Path,
    *,
    max_depth: int = TREE_MAX_DEPTH,
    max_entries: int = TREE_MAX_ENTRIES,
) -> str:
    """缩进目录树：目录在前、文件在后，各按名称排序；剪枝+限深+限条目。"""
    lines: List[str] = []
    truncated = False

    def walk(directory: Path, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth or truncated:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        dirs = [entry for entry in entries
                if not entry.is_symlink()
                and entry.is_dir() and not is_pruned_dir(entry.name)]
        files = [entry for entry in entries
                 if not entry.is_symlink()
                 and entry.is_file() and not is_hidden_file(entry.name)]
        for entry in dirs + files:
            if len(lines) >= max_entries:
                truncated = True
                return
            indent = "  " * (depth - 1)
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                walk(entry, depth + 1)
            else:
                lines.append(f"{indent}{entry.name}")

    walk(root, 1)
    if truncated:
        lines.append(f"…[目录树截断，仅显示前 {max_entries} 项]")
    return "\n".join(lines) or "（空工作区）"


# ---------------------------------------------------------------------------
# 项目规则文件
# ---------------------------------------------------------------------------

def read_agents_md(
    root: Path,
    home: Optional[Path] = None,
    *,
    max_chars: int = AGENTS_MD_MAX_CHARS,
) -> str:
    """全局 ~/.krelcode/AGENTS.md + 项目级规则；项目规则在后、优先级更高。"""
    home_dir = Path.home() if home is None else Path(home)
    sections: List[str] = []

    global_path = home_dir / GLOBAL_RULES_RELPATH
    global_text = _read_capped(global_path, max_chars)
    if global_text:
        sections.append(
            f"全局规则（来自 ~/.krelcode/AGENTS.md，务必遵守）:\n{global_text}")

    for name in AGENTS_MD_NAMES:
        project_path = root / name
        try:
            if not project_path.resolve().is_relative_to(root.resolve()):
                continue
        except OSError:
            continue
        project_text = _read_capped(project_path, max_chars)
        if project_text:
            sections.append(f"项目规则（来自 {name}，务必遵守）:\n{project_text}")
            break
    return "\n\n".join(sections)


def _read_capped(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# git 快照（asyncio subprocess，失败安全）
# ---------------------------------------------------------------------------

async def _git(root: Path, *args: str) -> Optional[str]:
    if shutil.which("git") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace").strip()


async def git_snapshot(root: Path) -> str:
    branch = await _git(root, "symbolic-ref", "--short", "HEAD")
    if branch is None:
        return ""
    parts = [f"git 分支: {branch}"]
    status = await _git(root, "status", "--porcelain")
    if status:
        parts.append(f"git 未提交变更:\n{truncate_middle(status, GIT_STATUS_MAX_CHARS)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 分层装配与预算
# ---------------------------------------------------------------------------

async def build_workspace_context_bundle(
    root: Path,
    home: Optional[Path] = None,
    *,
    options: Optional[ContextOptions] = None,
    extra_sections: Sequence[ContextSection] = (),
) -> WorkspaceContext:
    """收集、排序并在统一 token 预算内装配工作区上下文。"""
    root = Path(root).resolve()
    opts = options or ContextOptions()
    sections: List[ContextSection] = list(extra_sections)

    if opts.include_rules:
        rules = read_agents_md(
            root, home=home, max_chars=opts.rules_max_chars)
        if rules:
            sections.append(ContextSection(
                name="rules", content=rules, priority=10,
                source="AGENTS.md/KRELCODE.md"))

    if opts.include_git:
        git_part = await git_snapshot(root)
        if git_part:
            sections.append(ContextSection(
                name="git", content=git_part, priority=20, source="git"))

    if opts.include_tree:
        try:
            tree = render_tree(
                root,
                max_depth=opts.tree_max_depth,
                max_entries=opts.tree_max_entries,
            )
            sections.append(ContextSection(
                name="tree",
                content=f"工作区目录树 ({root}):\n{tree}",
                priority=50,
                source="filesystem",
            ))
        except OSError:
            pass

    content_budget = max(0, opts.max_tokens - BUNDLE_OVERHEAD_TOKENS)
    selected, omitted = _fit_sections(sections, content_budget)
    text = _render_bundle(selected)
    return WorkspaceContext(
        root=root,
        sections=tuple(selected),
        text=text,
        max_tokens=opts.max_tokens,
        total_tokens=estimate_text_tokens(text),
        omitted=tuple(omitted),
    )


def _fit_sections(
    sections: Sequence[ContextSection], max_tokens: int,
) -> tuple[List[ContextSection], List[str]]:
    """按 priority 分配预算；保留原顺序渲染，避免高优先级被低优先级挤掉。"""
    selected: List[tuple[int, ContextSection]] = []
    omitted: List[str] = []
    remaining = max_tokens
    separator_tokens = estimate_text_tokens("\n\n")
    ranked = sorted(enumerate(sections), key=lambda item: item[1].priority)
    for index, section in ranked:
        overhead = separator_tokens if selected else 0
        available = remaining - overhead
        if available < MIN_SECTION_TOKENS:
            omitted.append(section.name)
            continue
        tokens = section.tokens
        if tokens <= available:
            selected.append((index, section))
            remaining -= overhead + tokens
            continue
        trimmed = _truncate_to_tokens(section.content, available)
        if not trimmed:
            omitted.append(section.name)
            continue
        fitted = ContextSection(
            section.name, trimmed, section.priority, section.source)
        selected.append((index, fitted))
        remaining -= overhead + fitted.tokens
    selected.sort(key=lambda item: item[0])
    return [section for _, section in selected], omitted


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or estimate_text_tokens(text) <= max_tokens:
        return text
    reserve = estimate_text_tokens(TRUNCATION_MARKER)
    budget = max_tokens - reserve
    if budget < MIN_SECTION_TOKENS:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    if low <= 0:
        return ""
    return text[:low].rstrip() + TRUNCATION_MARKER


def _render_bundle(sections: Sequence[ContextSection]) -> str:
    if not sections:
        return ""
    body = "\n\n".join(section.content for section in sections)
    return "<workspace_context>\n" + body + "\n</workspace_context>"


async def build_workspace_context(
    root: Path,
    home: Optional[Path] = None,
    *,
    options: Optional[ContextOptions] = None,
    extra_sections: Sequence[ContextSection] = (),
) -> str:
    """兼容接口：返回可直接拼入 system prompt 的上下文文本。"""
    bundle = await build_workspace_context_bundle(
        root, home=home, options=options, extra_sections=extra_sections)
    return bundle.text
