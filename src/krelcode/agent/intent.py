"""本轮意图识别：在模型选择工具之前收紧任务空间。

这不是工具调用后的权限拦截。对于高置信度的“只解释明确文件”请求，
AgentLoop 会把结构化意图写入本轮 system context，并且只向模型提供
read_file 工具，避免模型先探索目录或读取 README/同级文件。模糊请求、
修改/运行等动作请求继续使用完整工具集。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

MODE_GENERAL = "general"
MODE_EXPLAIN_FILES = "explain_files"

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
_ATTACHED_FILE_RE = re.compile(
    r'<attached_file path="([^"]+)">.*?</attached_file>',
    re.DOTALL,
)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INTENT_TEXT_LIMIT = 2_000

_READ_INTENT_TERMS = (
    "解释", "讲解", "说明", "分析", "概述", "总结", "概括", "梳理",
    "介绍", "讲讲", "看看", "阅读", "逐行", "作用", "用途", "做什么",
    "干什么", "流程", "explain", "describe", "summarize", "walk through",
    "walk me through", "what does", "how does",
)
_ACTION_TERMS = (
    "修复", "修改", "改成", "实现", "添加", "新增", "删除", "重构", "优化",
    "测试", "运行", "执行", "安装", "提交", "部署", "生成", "创建",
    "fix", "edit", "modify", "implement", "add", "remove", "refactor",
    "test", "run", "execute", "install", "commit", "deploy", "build",
)
_ACTION_CONNECTOR_RE = re.compile(
    r"(?:并|然后|再|同时|随后|以及|且|and|then|also)\s*$",
    re.IGNORECASE,
)


def _has_term(text: str, term: str) -> bool:
    """中文按子串匹配；英文按完整单词匹配，避免文件名中的 run/test 误判。"""
    if term.isascii() and any(ch.isalpha() for ch in term):
        return re.search(
            rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE
        ) is not None
    return term.casefold() in text.casefold()


def _term_spans(text: str, terms: Tuple[str, ...]):
    """产出术语命中区间；英文仍按完整单词匹配。"""
    for term in terms:
        if term.isascii() and any(ch.isalpha() for ch in term):
            pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
        else:
            pattern = re.escape(term)
        for match in re.finditer(pattern, text, re.IGNORECASE):
            yield match.start(), match.end()


def _action_dominates_read(intent_text: str) -> bool:
    """区分“解释代码里的 run/test”与“解释后运行/测试”这类复合动作。"""
    read_spans = list(_term_spans(intent_text, _READ_INTENT_TERMS))
    if not read_spans:
        return False
    first_read = min(start for start, _ in read_spans)
    for start, _ in _term_spans(intent_text, _ACTION_TERMS):
        if start < first_read:
            return True
        prefix = intent_text[max(0, start - 12):start]
        if _ACTION_CONNECTOR_RE.search(prefix):
            return True
    return False


@dataclass(frozen=True)
class TurnIntent:
    """一次用户请求的结构化意图。"""

    mode: str = MODE_GENERAL
    target_files: Tuple[Path, ...] = ()

    @property
    def is_file_explanation(self) -> bool:
        return self.mode == MODE_EXPLAIN_FILES and bool(self.target_files)

    def labels(self, workspace_root: Path) -> Tuple[str, ...]:
        root = workspace_root.resolve()
        labels = []
        for path in self.target_files:
            try:
                labels.append(path.relative_to(root).as_posix())
            except ValueError:
                labels.append(str(path))
        return tuple(labels)

    def allows_tool(self, name: str) -> bool:
        """解释模式只暴露直接读取工具，不给模型目录探索入口。"""
        return not self.is_file_explanation or name == "read_file"

    def system_hint(self, workspace_root: Path) -> str:
        if not self.is_file_explanation:
            return ""
        targets = "\n".join(f"- `{label}`" for label in self.labels(workspace_root))
        return (
            '<turn_intent mode="explain_files" confidence="high">\n'
            "The user asks to explain only these explicitly named files:\n"
            f"{targets}\n"
            "Required behavior for this turn:\n"
            "1. If an <attached_file> block already contains a target, answer from it.\n"
            "2. Otherwise call read_file directly for the target file.\n"
            "3. Do not glob, grep, or inspect directories. Do not read README, config, "
            "test, or sibling files unless the user explicitly names another file.\n"
            "4. Do not infer code that was not read. If a missing dependency is "
            "essential, state the limitation briefly instead of expanding scope.\n"
            "</turn_intent>"
        )


def infer_turn_intent(workspace_root: Path, user_input: str) -> TurnIntent:
    """保守识别高置信度的“仅解释明确文件”请求。"""
    attached_paths = _ATTACHED_FILE_RE.findall(user_input)
    # 附件正文可能包含“修改/测试”等词，不能参与意图判断。
    prose = _ATTACHED_FILE_RE.sub(" ", user_input)
    # 评审/代码粘贴场景的大段 fenced code 不是用户指令，不参与意图判断。
    prose = _CODE_FENCE_RE.sub(" ", prose)[:_INTENT_TEXT_LIMIT]
    path_matches = _FILE_PATH_RE.findall(prose) + attached_paths
    intent_text = prose
    for match in path_matches:
        intent_text = intent_text.replace(match, " ", 1)

    has_read_intent = any(_has_term(intent_text, term) for term in _READ_INTENT_TERMS)
    if not has_read_intent or _action_dominates_read(intent_text):
        return TurnIntent()

    root = workspace_root.resolve()
    targets = []
    for match in path_matches:
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
        if resolved not in targets:
            targets.append(resolved)
    if not targets:
        return TurnIntent()
    return TurnIntent(mode=MODE_EXPLAIN_FILES, target_files=tuple(targets))
