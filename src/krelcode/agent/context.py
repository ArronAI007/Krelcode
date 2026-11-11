"""工作区上下文收集：让 agent 开局就"看得见"环境。

三部分内容（全部失败安全——任何一项拿不到就跳过，不影响启动）：
1. 目录树快照：剪枝隐藏/依赖目录，限深度与条目数
2. git 快照：当前分支 + porcelain 状态（无 git/非仓库则跳过）
3. AGENTS.md / KRELCODE.md 项目规则（主流约定，限字符数）

Step 7 会把这里升级为动态 token 预算管理；当前为启动时一次性快照。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import List, Optional

from ..tools.fsutils import is_hidden_file, is_pruned_dir, truncate_middle

TREE_MAX_DEPTH = 3
TREE_MAX_ENTRIES = 150
AGENTS_MD_MAX_CHARS = 8_000
GIT_STATUS_MAX_CHARS = 2_000
AGENTS_MD_NAMES = ("AGENTS.md", "KRELCODE.md")


# ---------------------------------------------------------------------------
# 目录树
# ---------------------------------------------------------------------------

def render_tree(root: Path) -> str:
    """缩进目录树：目录在前、文件在后，各按名称排序；剪枝+限深+限条目。"""
    lines: List[str] = []
    truncated = False

    def walk(directory: Path, depth: int) -> None:
        nonlocal truncated
        if depth > TREE_MAX_DEPTH or truncated:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        dirs = [e for e in entries if e.is_dir() and not is_pruned_dir(e.name)]
        files = [e for e in entries if e.is_file() and not is_hidden_file(e.name)]
        for entry in dirs + files:
            if len(lines) >= TREE_MAX_ENTRIES:
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
        lines.append(f"…[目录树截断，仅显示前 {TREE_MAX_ENTRIES} 项]")
    return "\n".join(lines) or "（空工作区）"


# ---------------------------------------------------------------------------
# 项目规则文件
# ---------------------------------------------------------------------------

def read_agents_md(root: Path) -> str:
    for name in AGENTS_MD_NAMES:
        path = root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            text = text[:AGENTS_MD_MAX_CHARS]
            return f"项目规则（来自 {name}，务必遵守）:\n{text}"
    return ""


# ---------------------------------------------------------------------------
# git 快照（asyncio subprocess，失败安全）
# ---------------------------------------------------------------------------

async def _git(root: Path, *args: str) -> Optional[str]:
    """跑一条 git 命令，成功返回 stdout（strip），失败返回 None。"""
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
    # symbolic-ref 对"已 init 未 commit"的仓库也能给出分支名
    branch = await _git(root, "symbolic-ref", "--short", "HEAD")
    if branch is None:
        return ""
    parts = [f"git 分支: {branch}"]
    status = await _git(root, "status", "--porcelain")
    if status:
        parts.append(f"git 未提交变更:\n{truncate_middle(status, GIT_STATUS_MAX_CHARS)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

async def build_workspace_context(root: Path) -> str:
    """组装 <workspace_context> 注入块；空工作区/全失败时返回空串。"""
    sections: List[str] = []
    try:
        sections.append(f"工作区目录树 ({root}):\n{render_tree(root)}")
    except OSError:
        pass
    git_part = await git_snapshot(root)
    if git_part:
        sections.append(git_part)
    agents_part = read_agents_md(root)
    if agents_part:
        sections.append(agents_part)
    if not sections:
        return ""
    return "<workspace_context>\n" + "\n\n".join(sections) + "\n</workspace_context>"
