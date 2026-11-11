"""glob：按模式匹配工作区内文件路径，新→旧排序（mtime）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ..base import ToolContext, ToolResult
from ..fsutils import filter_pruned
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace

MAX_RESULTS = 200


class GlobTool:
    name = "glob"
    description = (
        "按 glob 模式匹配工作区内文件，返回相对路径，按修改时间新→旧排序。"
        "递归匹配用 '**'（如 '**/*.py'）；可用 path 限定起始子目录。"
        "自动跳过隐藏与依赖目录。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，如 '**/*.py' 或 'tests/test_*.py'"},
            "path": {"type": "string", "description": "起始目录（相对工作区根，缺省全工作区）"},
        },
        "required": ["pattern"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return ToolResult(content="pattern 必须是非空字符串", is_error=True)
        if Path(pattern).is_absolute():
            return ToolResult(content="pattern 必须是相对模式（搜索范围固定在工作区内）", is_error=True)

        try:
            base = resolve_in_workspace(ctx, str(args.get("path") or "."))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not base.is_dir():
            return ToolResult(content=f"起始目录不存在: {base}", is_error=True)

        try:
            raw_matches = [p for p in base.glob(pattern) if p.is_file()]
        except (ValueError, OSError) as e:
            return ToolResult(content=f"非法 glob 模式: {e}", is_error=True)

        matches: List[Path] = filter_pruned(raw_matches, base)
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        total = len(matches)
        shown = matches[:MAX_RESULTS]

        root = ctx.workspace_root.resolve()
        rels = [str(p.relative_to(root)) if p.is_relative_to(root) else str(p) for p in shown]
        if not rels:
            return ToolResult(content=f"未找到匹配: {pattern!r}", data={"count": 0})
        note = f"\n[共 {total} 个匹配，仅显示最新 {MAX_RESULTS} 个]" if total > MAX_RESULTS else ""
        return ToolResult(content="\n".join(rels) + note, data={"count": total})
