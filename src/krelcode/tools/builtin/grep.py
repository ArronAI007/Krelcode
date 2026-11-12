"""grep：纯 Python 正则代码搜索（零依赖，不依赖系统 ripgrep）。

- 遍历工作区（或指定子目录），剪枝隐藏/依赖/产物目录
- 输出 `相对路径:行号: 行内容`（行内容截断）
- include 过滤（如 "*.py"）、ignore_case、max_results 上限
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Any, Dict, List

from ..base import ToolContext, ToolResult
from ..fsutils import is_hidden_file, is_pruned_dir
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace
from ..secret_guard import classify_secret_path

DEFAULT_MAX_RESULTS = 100
MAX_RESULT_LIMIT = 500
MAX_LINE_CHARS = 300


class GrepTool:
    name = "grep"
    description = (
        "在工作区内按正则搜索文件内容，输出 '路径:行号: 行内容'。"
        "可用 path 限定子目录、include 过滤文件名（如 '*.py'）、"
        "ignore_case 忽略大小写。自动跳过隐藏与依赖目录。"
    )
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
        max_results = _clamp_int(args.get("max_results"), DEFAULT_MAX_RESULTS, MAX_RESULT_LIMIT)

        matches: List[str] = []
        truncated = False
        files_scanned = 0
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not is_pruned_dir(d))
            for name in sorted(files):
                if is_hidden_file(name):
                    continue
                if include and not fnmatch.fnmatch(name, include):
                    continue
                file_path = os.path.join(root, name)
                if classify_secret_path(file_path, ctx.workspace_root) is not None:
                    continue  # 敏感文件不参与搜索，防 grep 命中密钥
                rel = os.path.relpath(file_path, ctx.workspace_root.resolve())
                files_scanned += 1
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, start=1):
                            if "\x00" in line:  # 二进制防呆
                                break
                            if regex.search(line):
                                snippet = line.rstrip("\n")
                                if len(snippet) > MAX_LINE_CHARS:
                                    snippet = snippet[:MAX_LINE_CHARS] + "…"
                                matches.append(f"{rel}:{lineno}: {snippet}")
                                if len(matches) >= max_results:
                                    truncated = True
                                    break
                except OSError:
                    continue
                if truncated:
                    break
            if truncated:
                break

        if not matches:
            return ToolResult(
                content=f"未找到匹配: {pattern!r}（扫描 {files_scanned} 个文件）",
                data={"count": 0},
            )
        note = f"\n[已达上限 {max_results} 条，结果被截断；可缩小 path/include 范围]" if truncated else ""
        return ToolResult(
            content="\n".join(matches) + note,
            data={"count": len(matches), "truncated": truncated},
        )


def _clamp_int(value: Any, default: int, limit: int) -> int:
    try:
        return max(1, min(int(value), limit))
    except (TypeError, ValueError):
        return default
