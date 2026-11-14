"""用户显式文件范围的轻量守卫。

纯解释/评审请求如果点名了已存在的源码文件，就没有必要先 glob 整个目录，
再读取 README 或同级文件。这里用保守规则识别这种只读意图，并在工具执行
前限制可见范围，既减少 token 消耗，也避免模型偏离用户指定的文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ..core.messages import ToolCall
from ..tools.base import ToolContext
from ..tools.paths import PathOutsideWorkspaceError, resolve_in_workspace

_FILE_EXTENSIONS = (
    "py|pyi|js|jsx|ts|tsx|java|go|rs|c|cc|cpp|h|hpp|rb|php|swift|kt|kts|"
    "scala|sh|bash|zsh|fish|sql|json|toml|yaml|yml|md|txt|cfg|ini|html|css|"
    "scss|vue|svelte|xml|lua|r|m|mm|cs|ex|exs|dart|ipynb"
)
_FILE_PATH_RE = re.compile(
    rf"(?<![\w/.-])("
    rf"(?:/(?:[\w.-]+/)+[\w.-]+\.(?:{_FILE_EXTENSIONS}))"
    rf"|(?:[\w.-]+/)*[\w.-]+\.(?:{_FILE_EXTENSIONS}))"
    rf"(?![\w/.-])",
    re.IGNORECASE,
)

_READ_INTENT_TERMS = (
    "解释", "讲解", "说明", "分析", "概述", "总结", "概括", "梳理",
    "介绍", "讲讲", "看看", "阅读", "逐行", "作用", "用途", "做什么",
    "干什么", "流程", "explain", "describe", "summarize", "review",
    "walk through", "walk me through", "what does", "how does", "read",
)
_ACTION_TERMS = (
    "修复", "修改", "改成", "实现", "添加", "新增", "删除", "重构", "优化",
    "测试", "运行", "执行", "安装", "提交", "部署", "生成", "创建",
    "fix", "edit", "modify", "implement", "add", "remove", "refactor",
    "test", "run", "execute", "install", "commit", "deploy", "build",
)


def _has_term(text: str, term: str) -> bool:
    """中文按子串匹配；英文按完整单词匹配，避免文件名中的 run/test 误判。"""
    if term.isascii() and any(ch.isalpha() for ch in term):
        return re.search(
            rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE
        ) is not None
    return term.casefold() in text.casefold()


@dataclass(frozen=True)
class ReadScope:
    """一次纯解释请求允许读取的显式文件集合。"""

    allowed_files: Tuple[Path, ...]

    def labels(self, workspace_root: Path) -> str:
        root = workspace_root.resolve()
        labels = []
        for path in self.allowed_files:
            try:
                labels.append(path.relative_to(root).as_posix())
            except ValueError:
                labels.append(str(path))
        return ", ".join(labels)


def infer_read_scope(workspace_root: Path, user_input: str) -> Optional[ReadScope]:
    """识别“只解释/评审明确文件”的请求；动作型任务不启用限制。"""
    matches = _FILE_PATH_RE.findall(user_input)
    # 动作词只能出现在用户指令中；文件名里的 run/test/build 不算动作。
    intent_text = user_input
    for match in matches:
        intent_text = intent_text.replace(match, " ", 1)
    if not any(_has_term(intent_text, term) for term in _READ_INTENT_TERMS):
        return None
    if any(_has_term(intent_text, term) for term in _ACTION_TERMS):
        return None

    root = workspace_root.resolve()
    allowed = []
    for match in matches:
        raw = match.rstrip(".,，。;；:：")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if not resolved.is_file() or (resolved != root and root not in resolved.parents):
            continue
        if resolved not in allowed:
            allowed.append(resolved)
    return ReadScope(tuple(allowed)) if allowed else None


def scope_violation(
    scope: Optional[ReadScope],
    tool_call: ToolCall,
    context: ToolContext,
) -> Optional[str]:
    """返回范围守卫的拒绝原因；允许执行时返回 None。"""
    if scope is None:
        return None
    allowed = scope.labels(context.workspace_root)
    if tool_call.name == "read_file":
        raw_path = str(tool_call.arguments.get("path") or "")
        try:
            target = resolve_in_workspace(context, raw_path)
        except (PathOutsideWorkspaceError, ValueError):
            # 交给工具自身返回更精确的路径错误。
            return None
        if target in scope.allowed_files:
            return None
        return (
            "当前请求只要求解释用户指定的文件，已阻止读取范围外文件。"
            f"允许读取: {allowed}；如确需其它文件，请先在消息中明确点出。"
        )
    return (
        f"当前请求只要求解释用户指定的文件，已阻止范围外工具调用 {tool_call.name}。"
        f"请直接读取: {allowed}；不要先搜索目录或读取 README/同级文件。"
    )
