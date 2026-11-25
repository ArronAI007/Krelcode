"""repo_map：增量、结构化的代码地图工具。

解析策略：
- Python：标准库 AST，精确提取 class/def（含方法与参数签名）
- JS/TS：安装可选 tree-sitter grammar 时用语法树，否则使用保守正则
- 索引：按文件 inode/size/mtime/ctime/mode 复用已解析符号，只重解析变更文件

输出带行号，LLM 拿到后可直接 `read_file(offset=行号)` 精读。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

from ...contracts.json import ToolExecutionData
from ...contracts.tool import ToolContext
from krelcode.contracts.tool import ToolResult, ToolTier
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace
from ..prompt import ToolPromptExample
from ..repo_index import MAX_FILE_BYTES, RepoMapIndex
from ..symbols import extract_symbols

MAX_TOTAL_CHARS = 20_000


class RepoMapTool:
    read_only = True
    tool_tier = ToolTier.DISCOVERABLE
    name = "repo_map"
    description = (
        "生成增量工作区代码地图：逐文件列出类/函数/方法/类型符号及行号。"
        "Python 使用 AST；JS/TS 优先 tree-sitter，未安装时自动使用正则。"
        "重复调用只重解析变化文件。可用 path 限定子目录。"
    )
    prompt = """\
Use repo_map to orient in an unfamiliar repository or when module structure and
symbol locations matter. Treat the map as an index: use returned paths and line
numbers to read only the definitions relevant to the task. Do not infer behavior
from symbol names alone. Repeated calls are incremental, so regenerate after a
material workspace change rather than relying on a stale map.
"""
    when_to_use = (
        "The repository or subsystem is unfamiliar and its symbol structure must be located.",
        "A broad but bounded overview is needed before selecting exact files.",
    )
    when_not_to_use = (
        "The relevant file and symbol are already known; use read_file or find_refs.",
        "A small focused content search would answer the question more cheaply.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Orient within an implementation package",
            input={"path": "src/krelcode/agent"},
            description=(
                "Build a focused structural map before choosing exact files, "
                "rather than reading the whole repository."
            ),
        ),
    )
    prompt_version = "2"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "起始目录（相对工作区根，缺省全工作区）"},
        },
        "required": [],
    }

    def __init__(self) -> None:
        self._index = RepoMapIndex()

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            base = resolve_in_workspace(ctx, str(args.get("path") or "."))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not base.is_dir():
            return ToolResult(content=f"目录不存在: {base}", is_error=True)

        root = ctx.workspace_root.resolve()
        snapshot = await asyncio.to_thread(self._index.build, base, root)
        out: List[str] = []
        total = 0
        file_count = 0
        truncated = False
        for entry in snapshot.files:
            if not entry.symbols:
                continue
            block = [f"{entry.relative_path}:"]
            block.extend(
                f"  {indent}L{lineno} {text}"
                for indent, lineno, text in entry.symbols
            )
            text = "\n".join(block) + "\n"
            if total + len(text) > MAX_TOTAL_CHARS:
                truncated = True
                break
            out.append(text)
            total += len(text)
            file_count += 1

        data: ToolExecutionData = {
            "files": file_count,
            "truncated": truncated,
            "max_file_bytes": MAX_FILE_BYTES,
            "incremental": {
                "parsed_files": snapshot.parsed_files,
                "reused_files": snapshot.reused_files,
                "removed_files": snapshot.removed_files,
            },
            "parser_backends": dict(snapshot.parser_backends),
        }
        if not out:
            return ToolResult(
                content=f"未发现可解析的源码文件: {base}",
                data=data,
            )
        body = "".join(out)
        if truncated:
            body += (
                f"…[代码地图截断于 {MAX_TOTAL_CHARS} 字符，"
                "可用 path 限定子目录]\n"
            )
        return ToolResult(content=body, data=data)


__all__ = ["MAX_FILE_BYTES", "MAX_TOTAL_CHARS", "RepoMapTool", "extract_symbols"]
