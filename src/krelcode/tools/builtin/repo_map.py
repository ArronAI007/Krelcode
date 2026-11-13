"""repo_map：工作区代码地图（Aider repo-map 思想的零依赖实现）。

不引 tree-sitter：
- Python 用标准库 ast 精确提取 class/def（含嵌套方法与参数签名）
- JS/TS 用正则提取 function/class/箭头函数常量
- 其他文件类型跳过（只列有符号的文件，控制预算）
输出带行号，LLM 拿到后可直接 read_file(offset=行号) 精读。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..base import ToolContext, ToolResult
from ..fsutils import is_hidden_file, is_pruned_dir
from ..paths import (
    PathOutsideWorkspaceError, resolve_in_workspace, safe_file_in_workspace,
)
from ..secret_guard import classify_secret_path

MAX_TOTAL_CHARS = 20_000
MAX_FILE_BYTES = 1_000_000  # 超过 1MB 的文件不解析
SIGNATURE_MAX_CHARS = 100

JS_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("), "const"),
]


class RepoMapTool:
    name = "repo_map"
    description = (
        "生成工作区代码地图：逐文件列出类/函数/方法符号及行号"
        "（Python 精确解析，JS/TS 正则提取）。用于快速了解仓库结构，"
        "之后可用 read_file 按行号精读。可用 path 限定子目录。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "起始目录（相对工作区根，缺省全工作区）"},
        },
        "required": [],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            base = resolve_in_workspace(ctx, str(args.get("path") or "."))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not base.is_dir():
            return ToolResult(content=f"目录不存在: {base}", is_error=True)

        root = ctx.workspace_root.resolve()
        out: List[str] = []
        total = 0
        file_count = 0
        truncated = False
        for path in _iter_source_files(base, ctx.workspace_root):
            symbols = extract_symbols(path)
            if not symbols:
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            block = [f"{rel}:"] + [f"  {indent}L{lineno} {text}" for indent, lineno, text in symbols]
            text = "\n".join(block) + "\n"
            if total + len(text) > MAX_TOTAL_CHARS:
                truncated = True
                break
            out.append(text)
            total += len(text)
            file_count += 1

        if not out:
            return ToolResult(content=f"未发现可解析的源码文件: {base}", data={"files": 0})
        body = "".join(out)
        if truncated:
            body += f"…[代码地图截断于 {MAX_TOTAL_CHARS} 字符，可用 path 限定子目录]\n"
        return ToolResult(content=body, data={"files": file_count, "truncated": truncated})


def _iter_source_files(base: Path, workspace_root: Path):
    """遍历源码文件（剪枝隐藏/依赖目录），路径排序保证输出稳定。"""
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(
            d for d in dirs
            if not is_pruned_dir(d) and not Path(root, d).is_symlink())
        for name in sorted(files):
            if is_hidden_file(name):
                continue
            if Path(name).suffix.lower() not in {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs"}:
                continue
            path = Path(root) / name
            if not safe_file_in_workspace(path, workspace_root):
                continue
            if classify_secret_path(path, workspace_root) is not None:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def extract_symbols(path: Path) -> List[Tuple[str, int, str]]:
    """返回 [(缩进, 行号, 符号描述)]；解析失败返回空表（跳过该文件）。"""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if path.suffix.lower() == ".py":
        return _python_symbols(source)
    return _js_symbols(source)


def _python_symbols(source: str) -> List[Tuple[str, int, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[Tuple[str, int, str]] = []

    def signature(node) -> str:
        names: List[str] = []
        a = node.args
        all_args = list(a.posonlyargs) + list(a.args)
        for arg in all_args:
            names.append(arg.arg)
        if a.vararg:
            names.append("*" + a.vararg.arg)
        elif a.kwonlyargs:
            names.append("*")
        for arg in a.kwonlyargs:
            names.append(arg.arg)
        if a.kwarg:
            names.append("**" + a.kwarg.arg)
        sig = f"{node.name}({', '.join(names)})"
        if len(sig) > SIGNATURE_MAX_CHARS:
            sig = sig[:SIGNATURE_MAX_CHARS] + "…)"
        return sig

    def walk(body, indent: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                out.append((indent, node.lineno, f"class {node.name}"))
                walk(node.body, indent + "  ")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
                out.append((indent, node.lineno, prefix + signature(node)))

    walk(tree.body, "")
    return out


def _js_symbols(source: str) -> List[Tuple[str, int, str]]:
    out: List[Tuple[str, int, str]] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for pattern, kind in JS_PATTERNS:
            m = pattern.match(line)
            if m:
                out.append(("", lineno, f"{kind} {m.group(1)}"))
                break
    return out
