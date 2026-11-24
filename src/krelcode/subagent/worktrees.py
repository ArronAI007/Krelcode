"""Isolated subagent git-worktree lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

from ..agent.context import _git
from ..core.git import ensure_local_exclude, git_is_repo
from .models import AgentHandle


class SubagentWorktreeManager:
    """Own worktree preparation, commit, and cleanup for one workspace."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root
        self._exclude_checked = False

    async def ensure_workspace_ready(self) -> None:
        if self._exclude_checked:
            return
        self._exclude_checked = True
        if await git_is_repo(self.workspace_root):
            await ensure_local_exclude(self.workspace_root, ".krelcode/")

    async def create(self, agent_id: str) -> Tuple[Path, str]:
        await self.ensure_workspace_ready()
        if not await git_is_repo(self.workspace_root):
            raise ValueError(
                "隔离子代理要求工作区是 git 仓库（先 git init，或不用 isolated）")
        branch = f"krelcode/agent-{agent_id}"
        path = self.workspace_root / ".krelcode" / "worktrees" / agent_id
        path.parent.mkdir(parents=True, exist_ok=True)
        out = await _git(
            self.workspace_root, "worktree", "add", str(path), "-b", branch)
        if out is None:
            raise ValueError(f"git worktree 创建失败（分支 {branch} 可能已存在）")
        return path, branch

    async def finalize(self, handle: AgentHandle) -> None:
        worktree = handle.worktree
        if worktree is None or not worktree.is_dir():
            return
        status = await _git(worktree, "status", "--porcelain")
        if not status:
            return
        await _git(worktree, "add", "-A")
        label = handle.task.strip().replace("\n", " ")[:60]
        await _git(
            worktree,
            "-c", "user.name=krelcode",
            "-c", "user.email=agent@krelcode.local",
            "commit", "-m", f"krelcode({handle.id}): {label}",
        )

    async def remove(self, handle: AgentHandle) -> None:
        if handle.worktree is None:
            return
        await _git(
            self.workspace_root,
            "worktree", "remove", "--force", str(handle.worktree),
        )
        await _git(self.workspace_root, "worktree", "prune")


__all__ = ["SubagentWorktreeManager"]
