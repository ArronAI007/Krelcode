"""文件系统遍历共享助手：目录剪枝规则（grep/glob 复用）。

规则对齐主流 coding agent：跳过隐藏目录（.git/.venv…）与依赖/产物目录，
避免搜索结果被垃圾淹没。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

PRUNED_DIRS = {
    "node_modules", "__pycache__", "dist", "build", "target",
    "venv", "env", ".venv", "site-packages",
}


def is_pruned_dir(name: str) -> bool:
    """隐藏目录（. 开头）与依赖/产物目录都剪掉。"""
    return name.startswith(".") or name in PRUNED_DIRS


def is_hidden_file(name: str) -> bool:
    return name.startswith(".")


def filter_pruned(paths: Iterable[Path], base: Path) -> List[Path]:
    """过滤文件列表中位于剪枝目录内或自身隐藏的路径。"""
    out = []
    for p in paths:
        try:
            rel_parts = p.relative_to(base).parts
        except ValueError:
            continue
        if any(is_pruned_dir(part) for part in rel_parts[:-1]):
            continue
        if is_hidden_file(rel_parts[-1]):
            continue
        out.append(p)
    return out


# truncate_middle 已上移至 core.textutil（agent 层也复用）；此处 re-export 保持兼容
from ..core.textutil import truncate_middle  # noqa: E402,F401
