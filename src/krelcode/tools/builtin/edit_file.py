"""edit_file：old_string→new_string 精确替换（主流验证过的方案）。

安全语义：
- old_string 必须在文件中**恰好出现一次**，否则报错（防止误改）
- replace_all=true 时显式替换全部出现
- old_string == new_string 视为无意义操作，报错
- 成功后返回 unified diff（截断），供 LLM/UI 确认改动
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List

from ..base import ToolContext, ToolResult
from ..paths import PathOutsideWorkspaceError, read_text_file, resolve_in_workspace

MAX_DIFF_LINES = 100


class EditFileTool:
    name = "edit_file"
    description = (
        "对工作区内文本文件做精确替换：把 old_string 替换为 new_string。"
        "old_string 必须与文件内容逐字符一致（含缩进/空行）且默认要求唯一匹配；"
        "匹配多处时会报错，确需全改时传 replace_all=true。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对路径基于工作区根）"},
            "old_string": {"type": "string", "description": "要被替换的原文（逐字符精确匹配）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "替换所有匹配（默认 false，仅允许唯一匹配）"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_in_workspace(ctx, str(args.get("path", "")))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)

        for key in ("old_string", "new_string"):
            if not isinstance(args.get(key), str):
                return ToolResult(content=f"{key} 必须是字符串", is_error=True)
        old: str = args["old_string"]
        new: str = args["new_string"]
        replace_all = bool(args.get("replace_all", False))

        if old == new:
            return ToolResult(content="old_string 与 new_string 相同，无需修改", is_error=True)
        if old == "":
            return ToolResult(content="old_string 不能为空（新建文件请用 write_file）", is_error=True)
        if not path.exists():
            return ToolResult(content=f"文件不存在: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(content=f"目标是目录，不是文件: {path}", is_error=True)

        try:
            text = read_text_file(path)
        except (ValueError, OSError) as e:
            return ToolResult(content=str(e), is_error=True)

        count = text.count(old)
        if count == 0:
            hint = _close_line_hint(text, old)
            return ToolResult(
                content=(f"未找到 old_string（需逐字符一致，注意缩进与空白）。"
                         f"{hint}文件: {path}"),
                is_error=True,
            )
        if count > 1 and not replace_all:
            lines_at = _occurrence_lines(text, old)
            return ToolResult(
                content=(
                    f"old_string 在文件中出现 {count} 次（第 {lines_at} 行），"
                    "为避免误改已中止。请扩大上下文使其唯一，或传 replace_all=true 全量替换。"
                ),
                is_error=True,
            )

        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as e:
            return ToolResult(content=f"写入失败: {e}", is_error=True)

        diff = _unified_diff(text, updated, str(path))
        return ToolResult(
            content=f"已编辑 {path}（替换 {count if replace_all else 1} 处）\n{diff}",
            data={"path": str(path), "replacements": count if replace_all else 1, "diff": diff},
        )


def _occurrence_lines(text: str, old: str, limit: int = 10) -> str:
    """old 每次出现的起始行号列表（字符串形式，超过 limit 截断）。"""
    lines: List[str] = []
    start = 0
    while True:
        pos = text.find(old, start)
        if pos < 0:
            break
        lines.append(str(text.count("\n", 0, pos) + 1))
        start = pos + max(1, len(old))
        if len(lines) >= limit:
            lines.append("…")
            break
    return "、".join(lines)


def _close_line_hint(text: str, old: str) -> str:
    """未匹配时，用首行做近似匹配给 LLM 修正线索（difflib，零依赖）。"""
    stripped = old.strip()
    if not stripped:
        return ""
    first = stripped.splitlines()[0].strip()
    if not first:
        return ""
    file_lines = text.splitlines()
    close = difflib.get_close_matches(
        first, [l.strip() for l in file_lines], n=3, cutoff=0.6)
    if not close:
        return ""
    entries = []
    for match in close:
        for i, line in enumerate(file_lines, start=1):
            if line.strip() == match:
                entries.append(f"L{i}: {match[:80]}")
                break
    return "最接近的行 → " + " | ".join(entries) + "。请用文件中的精确原文（含缩进）。"


def _unified_diff(before: str, after: str, path: str) -> str:
    lines: List[str] = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"…[diff 截断，共 {len(lines)} 行]"]
    return "\n".join(lines)
