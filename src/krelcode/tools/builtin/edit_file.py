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

from ...contracts.tool import ToolContext
from krelcode.contracts.tool import ToolResult
from ..paths import (
    FileChangedError,
    PathOutsideWorkspaceError,
    UnsafeFileError,
    atomic_write_text,
    read_text_file_with_snapshot,
    resolve_in_workspace,
)
from ..prompt import ToolPromptExample

MAX_DIFF_LINES = 100


class EditFileTool:
    name = "edit_file"
    description = (
        "对工作区内文本文件做精确替换：把 old_string 替换为 new_string。"
        "old_string 必须与文件内容逐字符一致（含缩进/空行）且默认要求唯一匹配；"
        "匹配多处时会报错，确需全改时传 replace_all=true。"
    )
    prompt = """\
Use edit_file for a localized change to an existing text file. Read the target
region immediately before editing and make the smallest coherent replacement
that satisfies the task.

- old_string must match the current file exactly, including whitespace and line
  endings. Keep enough surrounding context to make the match unique.
- Do not use replace_all unless changing every occurrence is explicitly intended
  and you have inspected each call site or occurrence.
- Preserve unrelated code, user edits, imports, formatting conventions, and
  behavior outside the requested scope. Do not use an edit as an opportunity for
  an unrelated refactor.
- A localized change must not silently rewrite the whole file. Use write_file
  only for a new file or an intentional whole-file replacement.
- After the tool returns, inspect the unified diff. Run the narrowest relevant
  verification when the edit changes executable behavior or a contract.
- File mutations must remain ordered. Do not attempt to parallel-edit the same
  file or race write_file against edit_file.

If old_string is missing or ambiguous, re-read the exact current content and
expand the context. Do not guess whitespace or force an ambiguous replacement.
"""
    when_to_use = (
        "An existing text file needs a bounded, exact replacement.",
        "The target region is known and can be made unique with surrounding context.",
    )
    when_not_to_use = (
        "Creating a new file or intentionally replacing the entire file; use write_file.",
        "The change requires broad generated rewrites or a mechanical transformation that should be performed by a reviewed script.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Make a unique localized edit",
            input={
                "path": "src/example/parser.py",
                "old_string": (
                    "def parse_header(value):\n"
                    "    return value.split(\",\")"
                ),
                "new_string": (
                    "def parse_header(value):\n"
                    "    return split_quoted(value, \",\")"
                ),
            },
            description=(
                "Read the function first, preserve surrounding code, and inspect the returned diff."
            ),
        ),
        ToolPromptExample(
            title="Replace every occurrence intentionally",
            input={
                "path": "src/example/constants.py",
                "old_string": "LEGACY_TIMEOUT",
                "new_string": "REQUEST_TIMEOUT",
                "replace_all": True,
            },
            description=(
                "Use replace_all only after verifying that every occurrence should change."
            ),
        ),
    )
    prompt_version = "1"
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
        try:
            text, snapshot = read_text_file_with_snapshot(path)
        except FileNotFoundError:
            return ToolResult(content=f"文件不存在: {path}", is_error=True)
        except (ValueError, OSError, UnsafeFileError, FileChangedError) as e:
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
            atomic_write_text(path, updated, expected=snapshot)
        except (OSError, UnsafeFileError, FileChangedError) as e:
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
