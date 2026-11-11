"""git 自动提交（Aider 风格）：每轮结束时若工作区有变更则提交一次。

- AutoCommitter 是 EventBus 的异步订阅者：RunStarted 记 label，
  RunFinished 触发 commit_if_dirty()
- 提交信息: "krelcode: <用户输入预览>"
- 启动时把 .krelcode/ 追加进 .git/info/exclude（仅本地生效，
  不污染用户的 .gitignore；幂等）
- 失败安全：非 git 仓库 / git 不可用 / 无变更 / 提交失败，全部静默跳过
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from ..agent.context import _git
from ..core.events import AgentEvent, RunFinished, RunStarted

LABEL_MAX_CHARS = 60
EXCLUDE_ENTRY = ".krelcode/"


class AutoCommitter:
    def __init__(self, workspace_root: Path) -> None:
        self.root = Path(workspace_root).resolve()
        self._label = ""
        self.last_commit_output: Optional[str] = None

    async def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            self._label = event.user_input.strip()[:LABEL_MAX_CHARS]
        elif isinstance(event, RunFinished):
            await self.commit_if_dirty()

    # -- 核心动作（也可单独调用/测试） --------------------------------------------------

    async def is_git_repo(self) -> bool:
        if shutil.which("git") is None:
            return False
        out = await _git(self.root, "rev-parse", "--is-inside-work-tree")
        return out == "true"

    async def commit_if_dirty(self) -> Optional[str]:
        """有未提交变更则 add -A + commit；返回 commit 输出（无操作返回 None）。"""
        if not await self.is_git_repo():
            return None
        status = await _git(self.root, "status", "--porcelain")
        if not status:
            return None
        await self.ensure_local_exclude()
        await _git(self.root, "add", "-A")
        message = f"krelcode: {self._label or 'auto-commit'}"
        out = await _git(self.root, "commit", "-m", message)
        self.last_commit_output = out
        return out

    async def ensure_local_exclude(self) -> bool:
        """把 .krelcode/ 写进 .git/info/exclude（幂等）；返回是否新增过。"""
        git_dir = await _git(self.root, "rev-parse", "--git-dir")
        if not git_dir:
            return False
        exclude_path = (self.root / git_dir / "info" / "exclude").resolve() \
            if not Path(git_dir).is_absolute() else Path(git_dir) / "info" / "exclude"
        try:
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
            lines = [l.strip() for l in existing.splitlines()]
            if EXCLUDE_ENTRY in lines:
                return False
            with open(exclude_path, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{EXCLUDE_ENTRY}\n")
            return True
        except OSError:
            return False
