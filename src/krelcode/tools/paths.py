"""共享路径助手：workspace 解析与越界防护（沙箱雏形）。

规则：
- 相对路径基于 ctx.workspace_root 解析
- 解析（含符号链接）后必须位于 workspace_root 内，否则拒绝
- Step 6 会把该规则升级为 ApprovalPolicy（可交互确认/持久化白名单），
  本模块保持为纯函数以便两层复用
"""

from __future__ import annotations

from pathlib import Path

from ..core.errors import KrelcodeError
from .base import ToolContext


class PathOutsideWorkspaceError(KrelcodeError):
    """目标路径越出工作区。"""

    def __init__(self, target: Path, root: Path) -> None:
        super().__init__(f"路径越出工作区: {target}（工作区: {root}）")
        self.target = target
        self.root = root


def resolve_in_workspace(ctx: ToolContext, raw: str) -> Path:
    """把工具入参路径解析为工作区内绝对路径；越界抛 PathOutsideWorkspaceError。"""
    if not raw or not str(raw).strip():
        raise ValueError("路径不能为空")
    root = ctx.workspace_root.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathOutsideWorkspaceError(resolved, root)
    return resolved


def read_text_file(path: Path) -> str:
    """读文本文件；二进制/非法编码转成清晰的工具错误。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"无法按 UTF-8 解码（可能是二进制文件）: {path.name} ({e})") from e
