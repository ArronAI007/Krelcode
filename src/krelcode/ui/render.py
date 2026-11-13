"""事件渲染器：EventBus 的旁路订阅者，负责流式输出与上色。

内核不知道渲染器的存在；渲染器只消费事件。
write/color 均可注入，测试无需真实终端。
"""

from __future__ import annotations

import os
import re
import sys
from typing import Callable, Optional

from ..core.redact import SecretRedactor
from ..core.events import (
    AgentEvent,
    ContextCompressed,
    ErrorOccurred,
    LLMStreamDelta,
    LLMThinkingDelta,
    PermissionDenied,
    RunFinished,
    RunStarted,
    SubagentFinished,
    SubagentSpawned,
    ToolCallCompleted,
    ToolCallStarted,
    UserSteered,
)

GREEN = "\x1b[32m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

# Claude Code / Hermes 风格的行首标记：⏺ 助手动作、⎿ 工具结果、✻ 思考
ASSISTANT_MARK = "⏺ "
RESULT_MARK = "⎿ "
THINKING_MARK = "✻ "
THINKING_COLLAPSED = f"{THINKING_MARK}Thinking… · 输入 /thinking 展开"

TOOL_CONTENT_PREVIEW = 200


def auto_color() -> bool:
    """tty 且未设置 NO_COLOR 时才上色。"""
    return bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")


def colorize_diff(text: str, enabled: bool = True) -> str:
    """unified diff 上色：+绿 -红 @@青。"""
    if not enabled:
        return text
    out = []
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            out.append(DIM + line + RESET)
        elif line.startswith("+"):
            out.append(GREEN + line + RESET)
        elif line.startswith("-"):
            out.append(RED + line + RESET)
        elif line.startswith("@@"):
            out.append(CYAN + line + RESET)
        else:
            out.append(line)
    return "\n".join(out)


_INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")
_BOLD_RE = re.compile(r"(\*\*[^*\n]+\*\*)")


def colorize_markdown(text: str, enabled: bool = True) -> str:
    """轻量终端 Markdown 上色，保留标题、列表、空行和 fenced code 结构。"""
    if not enabled or not text:
        return text
    out = []
    in_fence = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            out.append(DIM + line + RESET)
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(line)
            continue
        if re.match(r"^\s{0,3}#{1,6}\s+", line):
            out.append(BOLD + CYAN + line + RESET)
            continue
        list_match = re.match(r"^(\s*)([-*+]|\d+\.)(\s+.*)$", line)
        if list_match:
            prefix, marker, rest = list_match.groups()
            out.append(prefix + CYAN + marker + RESET + rest)
            continue
        quote_match = re.match(r"^(\s*>+)(.*)$", line)
        if quote_match:
            marker, rest = quote_match.groups()
            out.append(CYAN + marker + RESET + rest)
            continue
        line = _INLINE_CODE_RE.sub(
            lambda m: CYAN + m.group(1) + RESET, line)
        line = _BOLD_RE.sub(
            lambda m: BOLD + m.group(1) + RESET, line)
        out.append(line)
    return "\n".join(out)


def _default_write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


class StreamRenderer:
    """把事件流渲染到终端：LLM 文本增量流式打印，工具活动打状态行。"""

    def __init__(
        self,
        write: Optional[Callable[[str], None]] = None,
        color: Optional[bool] = None,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self._raw_write = write or _default_write
        self._redactor = redactor
        self._color = auto_color() if color is None else color
        self._streaming = False
        self._thinking = False
        self._thinking_summary_shown = False
        self._at_line_start = True
        # 仅 UI 层保留最近一轮完整 thinking；绝不写入 AgentLoop.history。
        self.thinking_text = ""

    def _write(self, s: str) -> None:
        # 所有终端输出都经此出口：脱敏后统一写出
        if self._redactor is not None:
            s = self._redactor.redact(s)
        self._raw_write(s)
        if s:
            self._at_line_start = s.endswith("\n")

    def _ensure_newline(self) -> None:
        """只在当前行仍有内容时换行，避免模型自带尾部换行时出现空行。"""
        if not self._at_line_start:
            self._write("\n")

    # EventBus handler（同步即可）
    def _mark(self, mark: str) -> str:
        return f"{CYAN}{mark}{RESET}" if self._color else mark

    def __call__(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            # 新一轮开始前清空缓存；上一轮的 thinking 在 idle 期间仍可展开。
            self._end_thinking()
            self._end_stream_only()
            self.thinking_text = ""
            self._thinking_summary_shown = False
            return
        if isinstance(event, LLMThinkingDelta):
            if not event.text:
                return
            if not self._thinking:
                self._end_stream_only()
                if self.thinking_text:
                    self.thinking_text = self.thinking_text.rstrip("\n") + "\n\n"
                self._thinking = True
            # 默认不输出 reasoning 正文；只缓存原始 Markdown，等待 /thinking。
            self.thinking_text += event.text
            return
        # 任何非思考事件：先把本轮折叠状态收束为一行。
        self._end_thinking()
        if isinstance(event, LLMStreamDelta):
            if not self._streaming:
                self._write(self._mark(ASSISTANT_MARK))
                self._streaming = True
            self._write(event.text)
        elif isinstance(event, ToolCallStarted):
            self.end_stream()
            args = event.tool_call.arguments
            self._write(
                f"  {self._mark(ASSISTANT_MARK)}{event.tool_call.name}({args})\n")
        elif isinstance(event, ToolCallCompleted):
            self.end_stream()
            self._render_tool_result(event)
        elif isinstance(event, PermissionDenied):
            self.end_stream()
            self._write(
                f"  {self._mark(RESULT_MARK)}✗ {event.tool_call.name} 被拒绝: {event.reason}\n")
        elif isinstance(event, UserSteered):
            self.end_stream()
            preview = event.text if len(event.text) <= 60 else event.text[:60] + "…"
            self._write(f"  {self._mark(RESULT_MARK)}插话已注入: {preview}\n")
        elif isinstance(event, SubagentSpawned):
            self.end_stream()
            task = event.task if len(event.task) <= 60 else event.task[:60] + "…"
            self._write(
                f"  {self._mark(ASSISTANT_MARK)}agent {event.agent_id} 启动: {task}\n")
        elif isinstance(event, SubagentFinished):
            self.end_stream()
            detail = f": {event.preview}" if event.preview else ""
            self._write(
                f"  {self._mark(RESULT_MARK)}agent {event.agent_id} "
                f"{event.status}{detail}\n")
        elif isinstance(event, ContextCompressed):
            self.end_stream()
            self._write(
                f"  {self._mark(RESULT_MARK)}历史已压缩: "
                f"{event.before_tokens} → {event.after_tokens} tokens\n")
        elif isinstance(event, ErrorOccurred):
            self.end_stream()
            self._write(
                f"  {self._mark(RESULT_MARK)}✗ {event.context}: {event.error}\n")
        elif isinstance(event, RunFinished):
            if self._streaming:
                self._ensure_newline()
                self._streaming = False
            else:
                # 未经流式的最终文本（如 max_steps/卡死中止的系统消息）
                self._write(f"{self._mark(ASSISTANT_MARK)}{event.final_text}")
                self._ensure_newline()

    def end_stream(self) -> None:
        """结束当前流式行（工具活动插入前调用）。"""
        self._end_thinking()
        self._end_stream_only()

    def _end_stream_only(self) -> None:
        if self._streaming:
            self._ensure_newline()
            self._streaming = False

    def _end_thinking(self) -> None:
        if not self._thinking:
            return
        self._thinking = False
        if not self._thinking_summary_shown and self.thinking_text.strip():
            summary = THINKING_COLLAPSED
            if self._color:
                summary = f"{DIM}{summary}{RESET}"
            self._write(summary + "\n")
            self._thinking_summary_shown = True

    def expand_thinking(self) -> bool:
        """把最近一轮 thinking 以多行 Markdown 输出；无内容时返回 False。"""
        if not self.thinking_text.strip():
            return False
        self.end_stream()
        header = "✻ Thinking（Markdown）"
        if self._color:
            header = f"{DIM}{header}{RESET}"
        self._write(header + "\n")
        self._write(colorize_markdown(self.thinking_text.strip("\n"), self._color))
        self._ensure_newline()
        return True

    def _render_tool_result(self, event: ToolCallCompleted) -> None:
        status = "✗" if event.is_error else "✓"
        name = event.tool_call.name
        prefix = f"  {self._mark(RESULT_MARK)}"
        if not event.is_error and name == "edit_file":
            # edit_file：首行结果 + 完整（工具已限 100 行）上色 diff
            lines = event.content.split("\n", 1)
            self._write(f"{prefix}{status} {name}: {lines[0]}\n")
            if len(lines) > 1:
                self._write(colorize_diff(lines[1], self._color) + "\n")
            return
        content = event.content.replace("\n", " ")
        if len(content) > TOOL_CONTENT_PREVIEW:
            content = content[:TOOL_CONTENT_PREVIEW] + "…"
        self._write(f"{prefix}{status} {name} → {content}\n")
