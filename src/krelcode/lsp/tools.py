"""Narrow model-facing tools backed by a configured LSP manager."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, cast

from ..contracts.json import ToolExecutionData
from ..contracts.tool import ToolContext, ToolResult, ToolTier
from ..tools.prompt import ToolPromptExample
from ..core.secret_guard import classify_secret_path
from .client import LSPError
from .manager import LSPManager

MAX_RESULT_CHARS = 24_000
MAX_ITEMS = 200


class _LSPTool:
    read_only = True
    tool_tier = ToolTier.DISCOVERABLE

    def __init__(self, manager: LSPManager) -> None:
        self.manager = manager

    def _path(self, args: Mapping[str, Any], ctx: ToolContext) -> Path:
        raw = str(args.get("path") or "").strip()
        if not raw:
            raise ValueError("path 不能为空")
        path = self.manager.resolve_path(raw)
        if path != Path(raw).expanduser() and not path.is_absolute():
            # ``manager.resolve_path`` already anchors relative paths at the
            # configured workspace; this branch is intentionally unreachable.
            pass
        if classify_secret_path(path, ctx.workspace_root) is not None:
            raise ValueError(f"拒绝将敏感文件发送给 LSP: {path.name}")
        if not path.is_file():
            raise ValueError(f"文件不存在: {path}")
        return path

    def _line_col(self, args: Mapping[str, Any]) -> tuple[int, int]:
        try:
            line = int(args.get("line", 1))
            character = int(args.get("character", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError("line/character 必须是整数") from exc
        if line < 1 or character < 1:
            raise ValueError("line/character 从 1 开始")
        # LSP uses zero-based positions.
        return line - 1, character - 1

    def unavailable(self) -> ToolResult:
        if not self.manager.enabled:
            return ToolResult(
                content="LSP 未启用。请在配置中设置 [lsp].enabled = true 并配置服务器。",
                is_error=True,
            )
        return ToolResult(content="没有为该系统文件类型配置 LSP server", is_error=True)

    def format_error(self, exc: BaseException) -> ToolResult:
        return ToolResult(
            content=f"LSP 操作失败: {type(exc).__name__}: {exc}",
            is_error=True,
        )


class LSPDiagnosticsTool(_LSPTool):
    name = "lsp_diagnostics"
    description = (
        "使用已配置的语言服务器检查单个工作区文件的编译/类型诊断。"
        "path 为相对工作区路径；返回 line:column、严重级别、源码与消息。"
        "该工具只读取文件并请求 LSP，不修改工作区。"
    )
    prompt = """\
Use lsp_diagnostics when the configured language server can provide stronger
compiler/type evidence than text inspection, especially after edits. Fix actionable
new diagnostics and distinguish pre-existing failures from regressions. A clean
diagnostic result does not replace relevant tests for runtime behavior.
"""
    when_to_use = (
        "A file uses a configured language server and compiler/type diagnostics would reduce uncertainty.",
        "An edit may have introduced syntax, type, import, or API errors.",
    )
    when_not_to_use = (
        "No language server is configured for the file.",
        "Behavioral verification requires tests or a runtime check instead.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Check one edited file for actionable diagnostics",
            input={"path": "src/krelcode/app.py", "severity": "warning"},
            description=(
                "Use compiler/type evidence on a known file, then distinguish new "
                "regressions from pre-existing diagnostics."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对工作区根）"},
            "severity": {
                "type": "string",
                "enum": ["error", "warning", "information", "hint", "all"],
                "description": "最低严重级别，默认 warning",
            },
        },
        "required": ["path"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if not self.manager.enabled:
                return self.unavailable()
            path = self._path(args, ctx)
            if self.manager.server_for_path(path) is None:
                return self.unavailable()
            result, client, uri, server = await self.manager.diagnostics_for(path)
            diagnostics = _extract_diagnostics(result)
            minimum = _severity_number(str(args.get("severity") or "warning"))
            filtered = [
                item for item in diagnostics
                if _diagnostic_severity(item.get("severity")) >= minimum
            ]
            filtered.sort(key=lambda item: (
                int((item.get("range") or {}).get("start", {}).get("line", 0)),
                int((item.get("range") or {}).get("start", {}).get("character", 0)),
                int(item.get("severity", 4)),
            ))
            if not filtered:
                return ToolResult(
                    content=f"✓ {_relative(path, ctx.workspace_root)} 未发现诊断问题"
                            f"（{server.name}）",
                    data={"path": str(path), "server": server.name,
                          "count": 0, "diagnostics": []},
                )
            lines = [
                f"{_relative(path, ctx.workspace_root)}: "
                f"{len(filtered)} 个诊断（{server.name}）"
            ]
            for item in filtered[:MAX_ITEMS]:
                lines.append(_format_diagnostic(
                    item, path, ctx.workspace_root))
            if len(filtered) > MAX_ITEMS:
                lines.append(f"…[只显示前 {MAX_ITEMS} 条]")
            content = _bounded("\n".join(lines))
            return ToolResult(
                content=content,
                is_error=any(int(item.get("severity", 1)) == 1
                             for item in filtered),
                data=cast(ToolExecutionData, {
                    "path": str(path), "server": server.name,
                    "count": len(filtered),
                    "diagnostics": filtered[:MAX_ITEMS],
                }),
            )
        except (ValueError, OSError, LSPError) as exc:
            return self.format_error(exc)


class LSPHoverTool(_LSPTool):
    name = "lsp_hover"
    description = (
        "获取文件中某个位置的类型、签名或文档说明。line/character 从 1 开始。"
        "适合在阅读代码时确认符号语义。"
    )
    prompt = """\
Use lsp_hover to resolve an ambiguous symbol's type, signature, or documentation
when surrounding text is insufficient. Request the smallest relevant position and
do not use hover as a broad repository scan.
"""
    when_to_use = (
        "A symbol's type, signature, or language-server documentation is ambiguous.",
        "A precise position can resolve an interpretation question.",
    )
    when_not_to_use = (
        "The relevant implementation body must be read or edited.",
        "No language server is configured for the file.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Resolve a symbol at a precise position",
            input={
                "path": "src/krelcode/app.py",
                "line": 120,
                "character": 16,
            },
            description=(
                "Query only the ambiguous symbol position when source context does "
                "not establish its type or signature."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "line": {"type": "integer", "minimum": 1, "description": "行号（从 1 开始）"},
            "character": {"type": "integer", "minimum": 1, "description": "列号（从 1 开始）"},
        },
        "required": ["path", "line", "character"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if not self.manager.enabled:
                return self.unavailable()
            path = self._path(args, ctx)
            if self.manager.server_for_path(path) is None:
                return self.unavailable()
            line, character = self._line_col(args)
            result, client, uri, server = await self.manager.request_for_file(
                path, "textDocument/hover", {
                    "position": {"line": line, "character": character},
                })
            if not result:
                return ToolResult(
                    content=f"（{server.name} 在 "
                            f"{_relative(path, ctx.workspace_root)}:"
                            f"{line + 1}:{character + 1} 没有 hover 信息）",
                    data={"path": str(path), "hover": None},
                )
            text = _hover_text(result)
            return ToolResult(
                content=_bounded(text or json.dumps(result, ensure_ascii=False)),
                data={"path": str(path), "server": server.name,
                      "hover": result},
            )
        except (ValueError, OSError, LSPError) as exc:
            return self.format_error(exc)


class LSPDefinitionTool(_LSPTool):
    name = "lsp_definition"
    description = (
        "查找符号定义位置。line/character 从 1 开始；返回工作区内的 "
        "file:line:column 位置列表。"
    )
    prompt = """\
Use lsp_definition to follow a symbol to its authoritative definition without a
broad repository scan. Read only the returned definition and nearby context needed
to understand behavior.
"""
    when_to_use = (
        "A symbol's implementation location is unknown and semantic resolution is needed.",
        "Following cross-file code should be more precise than text search.",
    )
    when_not_to_use = (
        "The definition location is already known.",
        "No language server is configured for the file.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Follow a symbol to its definition",
            input={
                "path": "src/krelcode/app.py",
                "line": 120,
                "character": 16,
            },
            description=(
                "Resolve a semantic definition location instead of scanning the "
                "repository by text."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "line": {"type": "integer", "minimum": 1},
            "character": {"type": "integer", "minimum": 1},
        },
        "required": ["path", "line", "character"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if not self.manager.enabled:
                return self.unavailable()
            path = self._path(args, ctx)
            if self.manager.server_for_path(path) is None:
                return self.unavailable()
            line, character = self._line_col(args)
            result, _, _, server = await self.manager.request_for_file(
                path, "textDocument/definition", {
                    "position": {"line": line, "character": character},
                })
            locations, outside = _locations(result, ctx.workspace_root)
            if not locations:
                suffix = f"；另有 {outside} 个工作区外位置被省略" if outside else ""
                return ToolResult(
                    content=f"（{server.name} 未找到定义{suffix}）",
                    data={"locations": [], "outside_workspace": outside},
                )
            lines = [f"{server.name} 定义:"]
            lines.extend(_format_location(item) for item in locations[:MAX_ITEMS])
            return ToolResult(
                content=_bounded("\n".join(lines)),
                data=cast(ToolExecutionData, {
                    "locations": locations[:MAX_ITEMS],
                    "outside_workspace": outside,
                    "server": server.name,
                }),
            )
        except (ValueError, OSError, LSPError) as exc:
            return self.format_error(exc)


class LSPReferencesTool(_LSPTool):
    name = "lsp_references"
    description = (
        "查找符号引用。line/character 从 1 开始；可通过 "
        "include_declaration 控制是否包含定义。"
    )
    prompt = """\
Use lsp_references before changing shared APIs, signatures, or behavior. Review
relevant callers and tests for behavioral assumptions instead of treating a count
as proof that every occurrence is equivalent. Do not edit matches mechanically.
"""
    when_to_use = (
        "A shared API, signature, or behavior may have multiple semantic callers.",
        "A language-server reference set is more reliable than text search.",
    )
    when_not_to_use = (
        "The search is for ordinary text or a non-semantic occurrence.",
        "No language server is configured for the file.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Review semantic references before an API change",
            input={
                "path": "src/krelcode/app.py",
                "line": 120,
                "character": 16,
                "include_declaration": True,
            },
            description=(
                "Inspect semantic callers and tests before changing a shared API; "
                "do not edit every location mechanically."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "line": {"type": "integer", "minimum": 1},
            "character": {"type": "integer", "minimum": 1},
            "include_declaration": {"type": "boolean", "default": True},
        },
        "required": ["path", "line", "character"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if not self.manager.enabled:
                return self.unavailable()
            path = self._path(args, ctx)
            if self.manager.server_for_path(path) is None:
                return self.unavailable()
            line, character = self._line_col(args)
            result, _, _, server = await self.manager.request_for_file(
                path, "textDocument/references", {
                    "position": {"line": line, "character": character},
                    "context": {
                        "includeDeclaration": bool(
                            args.get("include_declaration", True))
                    },
                })
            locations, outside = _locations(result, ctx.workspace_root)
            if not locations:
                suffix = f"；另有 {outside} 个工作区外位置被省略" if outside else ""
                return ToolResult(
                    content=f"（{server.name} 未找到引用{suffix}）",
                    data={"locations": [], "outside_workspace": outside},
                )
            lines = [f"{server.name} 引用（{len(locations)}）:"]
            lines.extend(_format_location(item) for item in locations[:MAX_ITEMS])
            if len(locations) > MAX_ITEMS:
                lines.append(f"…[只显示前 {MAX_ITEMS} 条]")
            return ToolResult(
                content=_bounded("\n".join(lines)),
                data=cast(ToolExecutionData, {
                    "locations": locations[:MAX_ITEMS],
                    "total": len(locations),
                    "outside_workspace": outside,
                    "server": server.name,
                }),
            )
        except (ValueError, OSError, LSPError) as exc:
            return self.format_error(exc)


def build_lsp_tools(manager: LSPManager) -> list[Any]:
    return [
        LSPDiagnosticsTool(manager),
        LSPHoverTool(manager),
        LSPDefinitionTool(manager),
        LSPReferencesTool(manager),
    ]


def _severity_number(value: str) -> int:
    # LSP severity ordering is inverted: 1=error ... 4=hint. ``all`` accepts
    # every numeric severity.
    return {
        "error": 1,
        "warning": 2,
        "information": 3,
        "info": 3,
        "hint": 4,
        "all": 4,
    }.get(value.lower(), 2)


def _diagnostic_severity(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return parsed if 1 <= parsed <= 4 else 1


def _severity_name(value: Any) -> str:
    return {1: "error", 2: "warning", 3: "information", 4: "hint"}.get(
        int(value or 1), "error")


def _extract_diagnostics(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    raw = result.get("items", result.get("diagnostics", []))
    return [dict(item) for item in raw or () if isinstance(item, dict)]


def _format_diagnostic(
    item: Mapping[str, Any], path: Path, workspace: Path,
) -> str:
    raw_range = item.get("range")
    range_value: Mapping[str, Any] = raw_range if isinstance(raw_range, dict) else {}
    raw_start = range_value.get("start")
    start: Mapping[str, Any] = raw_start if isinstance(raw_start, dict) else {}
    line = int(start.get("line", 0)) + 1
    column = int(start.get("character", 0)) + 1
    severity = _severity_name(item.get("severity"))
    source = str(item.get("source") or "").strip()
    code = item.get("code")
    detail = f" ({source}{':' if source and code is not None else ''}{code})" \
        if source or code is not None else ""
    message = str(item.get("message") or "").replace("\n", " ")
    return (
        f"{_relative(path, workspace)}:{line}:{column} "
        f"[{severity}] {message}{detail}"
    )


def _hover_text(result: Mapping[str, Any]) -> str:
    contents = result.get("contents")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        value = contents.get("value")
        return str(value or "")
    if isinstance(contents, list):
        parts: list[str] = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("value")
                if value is not None:
                    parts.append(str(value))
        return "\n\n".join(parts)
    return ""


def _locations(
    result: Any, workspace: Path,
) -> tuple[list[dict[str, Any]], int]:
    raw = result if isinstance(result, list) else ([result] if result else [])
    output: list[dict[str, Any]] = []
    outside = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("targetUri")
        range_value = item.get("range") or item.get("targetSelectionRange") or {}
        if not uri:
            continue
        try:
            path = _uri_path(str(uri))
            relative = path.relative_to(workspace)
        except (ValueError, OSError):
            outside += 1
            continue
        start = range_value.get("start") if isinstance(range_value, dict) else {}
        output.append({
            "path": relative.as_posix(),
            "line": int((start or {}).get("line", 0)) + 1,
            "character": int((start or {}).get("character", 0)) + 1,
            "uri": str(uri),
        })
    # deterministic and de-duplicated
    unique: dict[tuple[str, int, int], dict[str, Any]] = {}
    for item in output:
        unique[(item["path"], item["line"], item["character"])] = item
    return list(unique.values()), outside


def _format_location(item: Mapping[str, Any]) -> str:
    return f"{item['path']}:{item['line']}:{item['character']}"


def _uri_path(uri: str) -> Path:
    from .client import uri_to_path
    return uri_to_path(uri).resolve()


def _relative(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


def _bounded(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n…[LSP 结果已截断]"


__all__ = [
    "LSPDefinitionTool",
    "LSPDiagnosticsTool",
    "LSPHoverTool",
    "LSPReferencesTool",
    "build_lsp_tools",
]
