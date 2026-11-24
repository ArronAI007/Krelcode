"""check_syntax：批量 Python 语法检查（ast.parse，零依赖、不执行代码）。

用途：agent 编辑完一批文件后快速自检"至少能编译"，比跑测试/解释器便宜得多。
支持单文件或整目录（剪枝隐藏/依赖目录）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import ast

from ...contracts.tool import ToolContext
from ..base import ToolResult, ToolTier
from ..fsutils import is_hidden_file, is_pruned_dir
from ..paths import (
    PathOutsideWorkspaceError, resolve_in_workspace, safe_file_in_workspace,
)
from ..prompt import ToolPromptExample
from ..secret_guard import classify_secret_path

MAX_FILES = 1_000
MAX_ERRORS = 50


class CheckSyntaxTool:
    read_only = True
    tool_tier = ToolTier.DISCOVERABLE
    name = "check_syntax"
    description = (
        "检查工作区内 Python 文件的语法错误（ast 解析，不执行代码）。"
        "path 可以是单个 .py 文件或目录（缺省整个工作区）。"
        "适合在批量编辑后快速自检。"
    )
    prompt = """\
Use check_syntax as a fast Python-only parse check after syntax-sensitive edits
or when a low-cost first sanity check is useful. It does not execute code and does
not validate imports, types, behavior, lint, or tests. Follow it with the narrowest
relevant behavioral verification rather than treating a clean parse as completion.
"""
    when_to_use = (
        "Python files were edited and a fast syntax-only sanity check is useful.",
        "A batch of Python files must be checked without executing them.",
    )
    when_not_to_use = (
        "Behavior, types, imports, lint, or tests need verification.",
        "The target is not Python source.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Check edited Python files",
            input={"path": "src/krelcode/tools"},
            description=(
                "Run a bounded parse-only check after syntax-sensitive edits; "
                "follow it with behavioral verification."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件或目录（相对工作区根，缺省全工作区）"},
        },
        "required": [],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            target = resolve_in_workspace(ctx, str(args.get("path") or "."))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not target.exists():
            return ToolResult(content=f"路径不存在: {target}", is_error=True)

        if target.is_file():
            if (target.suffix == ".py"
                    and classify_secret_path(target, ctx.workspace_root) is None):
                py_files = [target]
            else:
                py_files = []
        else:
            py_files = list(_iter_py_files(target, ctx.workspace_root))
        if not py_files:
            return ToolResult(content="未找到 Python 文件")
        if len(py_files) > MAX_FILES:
            py_files = py_files[:MAX_FILES]

        root = ctx.workspace_root.resolve()
        errors: List[str] = []
        for path in py_files:
            try:
                ast.parse(path.read_text(encoding="utf-8", errors="ignore"),
                          filename=str(path))
            except SyntaxError as e:
                rel = _rel(path, root)
                errors.append(f"{rel}:{e.lineno or 0}:{e.offset or 0}: {e.msg}")
            except (OSError, ValueError) as e:
                errors.append(f"{_rel(path, root)}: 读取失败: {e}")
            if len(errors) >= MAX_ERRORS:
                errors.append(f"…[错误过多，仅显示前 {MAX_ERRORS} 条]")
                break

        if errors:
            return ToolResult(
                content=(f"发现 {len(errors)} 处语法问题（共检查 {len(py_files)} 个文件）:\n"
                         + "\n".join(errors)),
                is_error=True,
                data={"files": len(py_files), "errors": len(errors)})
        return ToolResult(
            content=f"✓ {len(py_files)} 个 Python 文件无语法错误",
            data={"files": len(py_files), "errors": 0})


def _iter_py_files(base: Path, workspace_root: Path) -> Iterator[Path]:
    import os
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(
            d for d in dirs
            if not is_pruned_dir(d) and not Path(root, d).is_symlink())
        for name in sorted(files):
            if not name.endswith(".py") or is_hidden_file(name):
                continue
            path = Path(root) / name
            if not safe_file_in_workspace(path, workspace_root):
                continue
            if classify_secret_path(path, workspace_root) is not None:
                continue
            yield path


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
