"""find_refs：ast 级 Python 符号引用查找（语法感知，区别于文本 grep）。

对每个 .py 文件做 ast.parse（失败静默跳过），分类收集：
- [def]    函数/类定义（FunctionDef/AsyncFunctionDef/ClassDef）
- [ref]    名字引用（Name.id）与属性访问（Attribute.attr，方法/属性调用）
- [import] 导入（ImportFrom 的 alias.name / alias.asname）

优势：不会被字符串/注释里的同名词干扰（grep 会），且能区分定义与使用。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..base import ToolContext, ToolResult
from ..fsutils import is_hidden_file, is_pruned_dir
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace

DEFAULT_MAX_RESULTS = 100
MAX_RESULT_LIMIT = 500
SNIPPET_MAX_CHARS = 120


class _RefVisitor(ast.NodeVisitor):
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.hits: List[Tuple[int, str]] = []   # (lineno, kind)

    def _add(self, lineno: int, kind: str) -> None:
        pair = (lineno, kind)
        if pair not in self.hits:
            self.hits.append(pair)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == self.symbol:
            self._add(node.lineno, "ref")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == self.symbol:
            self._add(node.lineno, "ref")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == self.symbol:
            self._add(node.lineno, "def")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == self.symbol:
            self._add(node.lineno, "def")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == self.symbol or alias.asname == self.symbol:
                self._add(node.lineno, "import")
        self.generic_visit(node)


class FindRefsTool:
    name = "find_refs"
    description = (
        "在工作区的 Python 文件中按 ast 语法查找符号引用（类/函数/变量/方法），"
        "分类为 [def] 定义 / [ref] 引用 / [import] 导入，输出 '路径:行号: [类型] 源码行'。"
        "比 grep 精确：不受字符串与注释里的同名词干扰。可用 path 限定子目录。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "要查找的符号名（精确匹配）"},
            "path": {"type": "string", "description": "起始目录（相对工作区根，缺省全工作区）"},
            "max_results": {"type": "integer", "description": f"最多返回条数（默认 {DEFAULT_MAX_RESULTS}）"},
        },
        "required": ["symbol"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        symbol = args.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return ToolResult(content="symbol 必须是非空字符串", is_error=True)
        symbol = symbol.strip()
        try:
            base = resolve_in_workspace(ctx, str(args.get("path") or "."))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not base.is_dir():
            return ToolResult(content=f"目录不存在: {base}", is_error=True)
        max_results = _clamp(args.get("max_results"))

        root = ctx.workspace_root.resolve()
        lines_out: List[str] = []
        truncated = False
        files_scanned = 0
        for py_file in _iter_py_files(base):
            files_scanned += 1
            hits = _scan_file(py_file, symbol)
            if not hits:
                continue
            try:
                source_lines = py_file.read_text(
                    encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            rel = _rel(py_file, root)
            for lineno, kind in sorted(hits):
                snippet = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
                if len(snippet) > SNIPPET_MAX_CHARS:
                    snippet = snippet[:SNIPPET_MAX_CHARS] + "…"
                lines_out.append(f"{rel}:{lineno}: [{kind}] {snippet}")
                if len(lines_out) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        if not lines_out:
            return ToolResult(
                content=f"未找到符号 {symbol!r} 的引用（扫描 {files_scanned} 个 Python 文件）",
                data={"count": 0})
        note = f"\n[已达上限 {max_results} 条；可缩小 path 范围]" if truncated else ""
        header = f"符号 {symbol!r} 共 {len(lines_out)} 处:"
        return ToolResult(
            content=header + "\n" + "\n".join(lines_out) + note,
            data={"count": len(lines_out), "truncated": truncated})


def _iter_py_files(base: Path):
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not is_pruned_dir(d))
        for name in sorted(files):
            if name.endswith(".py") and not is_hidden_file(name):
                yield Path(root) / name


def _scan_file(path: Path, symbol: str) -> List[Tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (SyntaxError, OSError, ValueError):
        return []          # 语法损坏/不可读的文件静默跳过
    visitor = _RefVisitor(symbol)
    visitor.visit(tree)
    return visitor.hits


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _clamp(value: Any) -> int:
    try:
        return max(1, min(int(value), MAX_RESULT_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS
