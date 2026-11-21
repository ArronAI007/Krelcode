"""glob：按模式匹配工作区内文件路径，新→旧排序（mtime）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ..base import ToolContext, ToolResult
from ..fsutils import filter_pruned
from ..paths import (
    PathOutsideWorkspaceError, resolve_in_workspace, safe_file_in_workspace,
)
from ..prompt import ToolPromptExample
from ..secret_guard import classify_secret_path

MAX_RESULTS = 200


class GlobTool:
    read_only = True
    name = "glob"
    description = (
        "按 glob 模式匹配工作区内文件，返回相对路径，按修改时间新→旧排序。"
        "递归匹配用 '**'（如 '**/*.py'）；可用 path 限定起始子目录。"
        "自动跳过隐藏与依赖目录。"
    )
    prompt = """\
Use glob when the filename, extension, or directory pattern is uncertain and a
bounded path search is needed. Prefer a specific base path and pattern over a
whole-repository enumeration. Inspect the smallest useful result set, then read
the relevant files. Do not use glob as a content search; use grep for content.
"""
    when_to_use = (
        "The existence or location of files matching a path pattern is unknown.",
        "A task must enumerate a bounded class of files such as tests or migrations.",
    )
    when_not_to_use = (
        "The user named an exact file; read it directly.",
        "The task is to find content rather than paths; use grep.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Locate tests by filename pattern",
            input={"pattern": "tests/test_*.py", "path": "tests"},
            description=(
                "Resolve candidate paths with a bounded glob, then inspect only "
                "the files relevant to the task."
            ),
        ),
    )
    prompt_version = "1"
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

        matches: List[Path] = [
            path for path in filter_pruned(raw_matches, base)
            if safe_file_in_workspace(path, ctx.workspace_root)
            and classify_secret_path(path, ctx.workspace_root) is None
        ]

        def mtime(path: Path) -> int:
            try:
                return path.stat().st_mtime_ns
            except OSError:
                return 0

        matches.sort(key=mtime, reverse=True)
        total = len(matches)
        shown = matches[:MAX_RESULTS]

        root = ctx.workspace_root.resolve()
        rels = [str(p.relative_to(root)) if p.is_relative_to(root) else str(p) for p in shown]
        if not rels:
            return ToolResult(content=f"未找到匹配: {pattern!r}", data={"count": 0})
        note = f"\n[共 {total} 个匹配，仅显示最新 {MAX_RESULTS} 个]" if total > MAX_RESULTS else ""
        return ToolResult(content="\n".join(rels) + note, data={"count": total})
