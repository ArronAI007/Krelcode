"""grep：ripgrep 加速、Python 等价降级的工作区正则搜索。

- 先用文件安全/敏感路径策略枚举候选文件，ripgrep 只接收这些文件
- 输出 `相对路径:行号: 行内容`（行内容截断）
- include 过滤（如 "*.py"）、ignore_case、max_results 上限
- rg 不可用、JSON 异常或 Python 正则超出 Rust regex 子集时自动回退
"""

from __future__ import annotations

import re
from typing import Any, Dict

from ...contracts.tool import ToolContext
from ..base import ToolResult
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace
from ..prompt import ToolPromptExample
from ..workspace_search import WorkspaceSearcher

DEFAULT_MAX_RESULTS = 100
MAX_RESULT_LIMIT = 500


class GrepTool:
    read_only = True
    name = "grep"
    description = (
        "在工作区内按正则搜索文件内容，输出 '路径:行号: 行内容'。"
        "优先使用 ripgrep；不可用或不兼容时自动回退到内置搜索。"
        "可用 path 限定子目录、include 过滤文件名（如 '*.py'）、"
        "ignore_case 忽略大小写。自动跳过隐藏、依赖与敏感文件。"
    )
    prompt = """\
Use grep to locate known text, identifiers, errors, call sites, or patterns in
workspace files. Start with the narrowest useful path and include filter; widen
the search only when the focused query is insufficient. Treat matches as pointers
to evidence, not as a substitute for reading the relevant file section. Do not
use grep for every task or dump an unbounded result set. Runtime secret-file
filtering cannot be bypassed by the ripgrep acceleration path.
"""
    when_to_use = (
        "The content to find is known but its file location is not.",
        "An error message, identifier, call site, or text fragment must be located.",
    )
    when_not_to_use = (
        "The user already named the target file and a direct read is more precise.",
        "A semantic symbol/reference query is better served by find_refs or an LSP tool.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Find a known pattern in Python sources",
            input={
                "pattern": "ToolPromptSpec\\(",
                "path": "src",
                "include": "*.py",
                "ignore_case": False,
                "max_results": 30,
            },
            description=(
                "Bound the search to the relevant subtree and a focused pattern; "
                "read promising matches next."
            ),
        ),
    )
    prompt_version = "2"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式（Python re 语法）"},
            "path": {"type": "string", "description": "搜索起始目录（相对工作区根，缺省全工作区）"},
            "include": {"type": "string", "description": "文件名 glob 过滤，如 '*.py'"},
            "ignore_case": {"type": "boolean", "description": "忽略大小写（默认 false）"},
            "max_results": {"type": "integer", "description": f"最多返回条数（默认 {DEFAULT_MAX_RESULTS}，上限 {MAX_RESULT_LIMIT}）"},
        },
        "required": ["pattern"],
    }

    def __init__(self, *, backend: str = "auto") -> None:
        self._searcher = WorkspaceSearcher(backend=backend)

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(content="pattern 必须是非空字符串", is_error=True)
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(content=f"非法正则: {e}", is_error=True)

        try:
            base = resolve_in_workspace(ctx, str(args.get("path") or "."))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not base.is_dir():
            return ToolResult(content=f"搜索目录不存在: {base}", is_error=True)

        include = args.get("include")
        if include is not None and not isinstance(include, str):
            return ToolResult(content="include 必须是字符串", is_error=True)
        max_results = _clamp_int(
            args.get("max_results"), DEFAULT_MAX_RESULTS, MAX_RESULT_LIMIT)

        batch = await self._searcher.search(
            pattern=pattern,
            regex=regex,
            base=base,
            workspace_root=ctx.workspace_root,
            include=include,
            ignore_case=bool(args.get("ignore_case")),
            max_results=max_results,
            abort=ctx.abort,
        )
        data = {
            "count": len(batch.matches),
            "truncated": batch.truncated,
            "files_scanned": batch.files_scanned,
            "backend": batch.backend,
        }
        if not batch.matches:
            return ToolResult(
                content=(
                    f"未找到匹配: {pattern!r}"
                    f"（扫描 {batch.files_scanned} 个文件）"
                ),
                data=data,
            )
        rendered = [
            f"{match.path}:{match.line}: {match.text}"
            for match in batch.matches
        ]
        note = (
            f"\n[已达上限 {max_results} 条，结果被截断；可缩小 path/include 范围]"
            if batch.truncated else ""
        )
        return ToolResult(content="\n".join(rendered) + note, data=data)


def _clamp_int(value: Any, default: int, limit: int) -> int:
    try:
        return max(1, min(int(value), limit))
    except (TypeError, ValueError):
        return default
