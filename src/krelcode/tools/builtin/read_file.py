"""read_file：带行号读取文本文件，支持 offset/limit 分段与多级截断。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ...contracts.json import ToolExecutionData
from ...contracts.tool import ToolContext
from krelcode.contracts.tool import ToolResult
from ..paths import (
    FileChangedError,
    PathOutsideWorkspaceError,
    UnsafeFileError,
    read_text_file,
    resolve_in_workspace,
)
from ...core.secret_guard import classify_secret_path, redact_secret_text
from ..prompt import ToolPromptExample

MAX_LINES = 2000          # 单次最多返回行数（可被 limit 覆盖到更小）
MAX_LINE_CHARS = 2000     # 单行超长截断
MAX_TOTAL_CHARS = 50_000  # 总输出字符上限（防止撑爆上下文；Step 7 治理）


class ReadFileTool:
    read_only = True
    name = "read_file"
    description = (
        "读取工作区内的文本文件，输出带行号（格式: '行号→内容'）。"
        "可用 offset（起始行，从 1 计）与 limit（行数）分段读取大文件。"
    )
    prompt = """\
Use read_file to inspect the exact file content needed to answer the user or
make a safe edit. Start with the file the user named; do not read unrelated
siblings merely to add background. For a large file, use offset and limit to
stage the read around the relevant symbol or line range. Reuse content already
present in context and do not reread an unchanged file unless its state is
uncertain. Independent reads of known files may run in parallel. Treat line
numbers and truncation notices as boundaries: continue with the indicated offset
when more context is needed. Sensitive-file protections and redaction are part
of the runtime contract and must not be bypassed.
"""
    when_to_use = (
        "The task needs exact file content, line numbers, or a target region to edit.",
        "A focused search result needs surrounding source context.",
    )
    when_not_to_use = (
        "The path or filename is still unknown; use glob or grep first.",
        "A broad repository summary is needed; use repo_map or grep before reading files.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Read a focused region",
            input={"path": "src/example/parser.py", "offset": 120, "limit": 80},
            description="Read enough context around the known symbol without loading the whole file.",
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对路径基于工作区根）"},
            "offset": {"type": "integer", "minimum": 1, "description": "起始行号（从 1 计，缺省 1）"},
            "limit": {"type": "integer", "minimum": 1, "description": f"最多读取行数（缺省 {MAX_LINES}）"},
        },
        "required": ["path"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_in_workspace(ctx, str(args.get("path", "")))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)

        if not path.exists():
            return ToolResult(content=f"文件不存在: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(content=f"目标是目录，不是文件: {path}", is_error=True)

        category = classify_secret_path(path, ctx.workspace_root)
        if category == "block":
            return ToolResult(content=f"拒绝读取敏感文件（凭据/密钥）: {path.name}", is_error=True)

        try:
            text = read_text_file(path)
        except (ValueError, OSError, UnsafeFileError, FileChangedError) as e:
            return ToolResult(content=str(e), is_error=True)

        redacted = category in ("redact_env", "redact_toml")
        if category == "redact_env":
            text = redact_secret_text(text, category)
        elif category == "redact_toml":
            text = redact_secret_text(text, category)

        offset = _positive_int(args.get("offset"), 1)
        limit = min(_positive_int(args.get("limit"), MAX_LINES), MAX_LINES)
        data: ToolExecutionData = {
            "path": str(path),
            "total_lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        }
        if redacted:
            data["redacted"] = True
        return ToolResult(content=_render(text, offset, limit), data=data)


def _positive_int(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if n >= 1 else default
    except (TypeError, ValueError):
        return default


def _render(text: str, offset: int, limit: int) -> str:
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return "（空文件）"
    if offset > total:
        return f"offset={offset} 超出文件总行数 {total}"

    out = []
    chars = 0
    shown = 0
    truncated_total = False
    for i in range(offset - 1, min(offset - 1 + limit, total)):
        line = lines[i]
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f"…[行截断，原长 {len(lines[i])} 字符]"
        rendered = f"{i + 1:6d}→{line}"
        if chars + len(rendered) > MAX_TOTAL_CHARS:
            truncated_total = True
            break
        out.append(rendered)
        chars += len(rendered) + 1
        shown += 1

    notes = []
    last_shown = offset - 1 + shown
    if truncated_total:
        notes.append(f"[输出已达 {MAX_TOTAL_CHARS} 字符上限，截断于第 {last_shown} 行]")
    if last_shown < total:
        notes.append(f"[显示第 {offset}-{last_shown} 行，共 {total} 行；继续读用 offset={last_shown + 1}]")
    return "\n".join(out) + ("\n" + "\n".join(notes) if notes else "")
