"""write_file：覆盖写/新建文件，自动创建父目录。"""

from __future__ import annotations

from typing import Any, Dict

from ...contracts.tool import ToolContext
from krelcode.contracts.tool import ToolResult
from ..paths import (
    FileChangedError,
    PathOutsideWorkspaceError,
    UnsafeFileError,
    atomic_write_text,
    resolve_in_workspace,
)
from ..prompt import ToolPromptExample


class WriteFileTool:
    name = "write_file"
    description = (
        "把工作区内文件覆盖写为给定内容（文件不存在则创建，自动建父目录）。"
        "局部修改请优先用 edit_file，避免整文件重写。"
    )
    prompt = """\
Use write_file to create a new file or intentionally replace an entire existing
file when the full final content is known.

- Read an existing target before overwriting it. Preserve content that the task
  did not ask to change, along with the repository's encoding and newline style.
- For a localized change, use edit_file instead of rewriting the whole file.
- Do not overwrite generated, vendored, lock, configuration, or user-owned files
  unless they are in scope and their regeneration/ownership rules are understood.
- The content argument must be the complete final file body, not a patch or
  fragment.
- After writing, inspect the result and run the narrowest relevant verification.
- File mutations must remain ordered. Do not race write_file against another
  edit or command that mutates the same path.
"""
    when_to_use = (
        "A new text file must be created with a complete initial body.",
        "An existing file intentionally needs a complete replacement and the result is fully known.",
    )
    when_not_to_use = (
        "A localized edit can safely express the change with edit_file.",
        "The complete final content is unknown or would discard unrelated user work.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Create a new source file",
            input={
                "path": "src/example/widget.py",
                "content": (
                    "class Widget:\n"
                    "    \"\"\"Minimal widget implementation.\"\"\"\n"
                ),
            },
            description="Provide the complete final file body for a new file.",
        ),
    )
    prompt_version = "1"
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
            atomic_write_text(path, content)
        except (OSError, UnsafeFileError, FileChangedError) as e:
            return ToolResult(content=f"写入失败: {e}", is_error=True)

        verb = "覆盖" if existed else "创建"
        n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(
            content=f"已{verb} {path}（{len(content.encode('utf-8'))} 字节，{n_lines} 行）",
            data={"path": str(path), "created": not existed, "bytes": len(content.encode("utf-8"))},
        )
