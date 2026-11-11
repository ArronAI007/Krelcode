"""write_file：覆盖写/新建文件，自动创建父目录。"""

from __future__ import annotations

from typing import Any, Dict

from ..base import ToolContext, ToolResult
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace


class WriteFileTool:
    name = "write_file"
    description = (
        "把工作区内文件覆盖写为给定内容（文件不存在则创建，自动建父目录）。"
        "局部修改请优先用 edit_file，避免整文件重写。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对路径基于工作区根）"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_in_workspace(ctx, str(args.get("path", "")))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)

        content = args.get("content")
        if not isinstance(content, str):
            return ToolResult(content="content 必须是字符串", is_error=True)
        if path.is_dir():
            return ToolResult(content=f"目标是已存在的目录: {path}", is_error=True)

        existed = path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(content=f"写入失败: {e}", is_error=True)

        verb = "覆盖" if existed else "创建"
        n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(
            content=f"已{verb} {path}（{len(content.encode('utf-8'))} 字节，{n_lines} 行）",
            data={"path": str(path), "created": not existed, "bytes": len(content.encode("utf-8"))},
        )
