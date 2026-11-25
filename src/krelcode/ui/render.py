"""事件渲染器：EventBus 的旁路订阅者，负责流式输出与上色。

内核不知道渲染器的存在；渲染器只消费事件。
write/color 均可注入，测试无需真实终端。
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional

from ..core.redact import SecretRedactor
from ..core.messages import ToolCall
from ..core.events import (
    AgentEvent,
    ContextAssembled,
    ContextCompressed,
    ContextMicrocompacted,
    ErrorOccurred,
    HookTriggered,
    LLMStreamDelta,
    LLMThinkingDelta,
    PermissionDenied,
    PlanUpdated,
    QueryPhaseChanged,
    RunFinished,
    RunStarted,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
    UserSteered,
)

GREEN = "\x1b[32m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
HERMES_GOLD = "\x1b[38;2;255;215;0m"
HERMES_AMBER = "\x1b[38;2;255;191;0m"
HERMES_BRONZE = "\x1b[38;2;205;127;50m"
HERMES_GOLD_256 = "\x1b[38;5;220m"
HERMES_AMBER_256 = "\x1b[38;5;214m"
HERMES_BRONZE_256 = "\x1b[38;5;173m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"
TOOL_PANEL_BG = "\x1b[48;5;236m"
TOOL_PANEL_FG = "\x1b[38;5;252m"

# Claude Code / Hermes 风格的行首标记：⏺ 助手动作、⎿ 工具结果、✻ 思考
ASSISTANT_MARK = "⏺ "
RESULT_MARK = "⎿ "
THINKING_MARK = "✻ "
THINKING_LIVE = f"{THINKING_MARK}意图识别中…"
THINKING_COLLAPSED = (
    f"{THINKING_MARK}意图识别完成（正文已折叠，/thinking 展开）"
)

TOOL_ARGUMENT_INLINE_LIMIT = 100
TOOL_ARGUMENT_PREVIEW_LINES = 8
TOOL_ARGUMENT_PREVIEW_CHARS = 1200
TOOL_OUTPUT_EXPANDED_LINES = 120
TOOL_OUTPUT_EXPANDED_CHARS = 12_000
TOOL_OUTPUT_DISPLAY_WIDTH = 120
TOOL_PANEL_WIDTH = 78
TOOL_OUTPUT_INDENT = "    "
REWRITE_LINE = "\r\x1b[2K"


@dataclass
class ToolOutputRecord:
    """UI 层工具输出记录；正文默认折叠，按编号手动展开。"""

    index: int
    tool_call: ToolCall
    content: str = ""
    is_error: Optional[bool] = None
    expanded: bool = False
    started_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    progress: str = ""
    last_output: str = ""


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


def _format_json_value(value: object) -> str:
    """用 JSON 风格显示工具参数；非 JSON 值回退到 repr。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    except (TypeError, ValueError):
        return repr(value)


def _clip_preview(
    text: str,
    max_lines: int,
    max_chars: int,
    label: str,
) -> str:
    """按行和字符双重上限裁剪预览，明确标记被省略的内容。"""
    normalized = text.rstrip("\n")
    lines = normalized.splitlines()
    if not lines:
        return "（空）"
    clipped_lines = lines[:max_lines]
    clipped = "\n".join(clipped_lines)
    truncated = len(lines) > max_lines
    if len(clipped) > max_chars:
        clipped = clipped[:max_chars].rstrip()
        truncated = True
    if truncated:
        clipped += f"\n…（{label}已截断，共 {len(lines)} 行 / {len(normalized)} 字符）"
    return clipped


def _wrap_tool_content(text: str, width: int = TOOL_OUTPUT_DISPLAY_WIDTH) -> list[str]:
    """只对超宽物理行硬换行，保留原行边界和全部空白。"""
    lines = []
    for line in text.splitlines() or [""]:
        if not line:
            lines.append("")
            continue
        lines.extend(line[start:start + width] for start in range(0, len(line), width))
    return lines


def _truncate_text(text: str, limit: int) -> str:
    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    printable = "".join(
        char for char in plain
        if char.isprintable() or char == "\t"
    )
    if len(printable) <= limit:
        return printable
    return printable[:max(0, limit - 1)] + "…"


def _visible_width(text: str) -> int:
    """终端可见宽度；忽略 ANSI，CJK 宽字符按 2 列计算。"""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in plain)


def _format_tool_arguments(arguments: Mapping[str, object]) -> str:
    """短参数用近似 Python 调用语法；长参数和多行文本展开并缩进。"""
    if not arguments:
        return ""
    inline = ", ".join(
        f"{key}={_format_json_value(value)}"
        for key, value in arguments.items()
    )
    has_multiline = any(
        isinstance(value, str) and "\n" in value
        for value in arguments.values()
    )
    if not has_multiline and len(inline) <= TOOL_ARGUMENT_INLINE_LIMIT:
        return inline

    lines = []
    for key, value in arguments.items():
        if isinstance(value, str) and "\n" in value:
            lines.append(f"{key}:")
            preview = _clip_preview(
                value,
                TOOL_ARGUMENT_PREVIEW_LINES,
                TOOL_ARGUMENT_PREVIEW_CHARS,
                "参数",
            )
            lines.extend(f"  {line}" for line in preview.splitlines())
            continue
        rendered = f"{key}={_format_json_value(value)}"
        lines.extend(textwrap.wrap(
            rendered,
            width=TOOL_ARGUMENT_INLINE_LIMIT - 8,
            subsequent_indent="    ",
            break_long_words=True,
            break_on_hyphens=False,
        ) or [rendered])
    return "\n".join(lines)


class StreamRenderer:
    """把事件流渲染到终端：LLM 文本增量流式打印，工具活动打状态行。"""

    def __init__(
        self,
        write: Optional[Callable[[str], None]] = None,
        color: Optional[bool] = None,
        redactor: Optional[SecretRedactor] = None,
        on_tool_line: Optional[Callable[[int], None]] = None,
        tool_lines_clickable: bool = False,
    ) -> None:
        self._raw_write = write or _default_write
        self._redactor = redactor
        self._color = auto_color() if color is None else color
        self._streaming = False
        self._thinking = False
        self._thinking_summary_shown = False
        self._thinking_line_live = False
        self._at_line_start = True
        self._on_tool_line = on_tool_line
        self._tool_lines_clickable = tool_lines_clickable
        # 仅 UI 层保留最近一轮完整 thinking；绝不写入 AgentLoop.history。
        self.thinking_text = ""
        self.tool_outputs: list[ToolOutputRecord] = []
        self._tool_by_call_id: Dict[str, ToolOutputRecord] = {}
        self._live_tool_key: Optional[str] = None

    @property
    def output_active(self) -> bool:
        """Whether model text or reasoning is currently being written."""
        return (
            self._streaming
            or self._thinking
            or self._live_tool_key is not None
        )

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
            self._finish_live_tool_line()
            self._end_thinking()
            self._end_stream_only()
            self.thinking_text = ""
            self._thinking_summary_shown = False
            # 工具调用 ID 只在单轮内保证唯一；新轮次必须重新映射，
            # 否则常见的 "c1" 会错误复用上一轮输出。
            self._tool_by_call_id.clear()
            return
        if isinstance(event, LLMThinkingDelta):
            if not event.text:
                return
            if not self._thinking:
                self._end_stream_only()
                if self.thinking_text:
                    self.thinking_text = self.thinking_text.rstrip("\n") + "\n\n"
                self._thinking = True
                if not self._thinking_summary_shown:
                    self._thinking_line_live = True
                    line = f"{DIM}{THINKING_LIVE}{RESET}" if self._color else THINKING_LIVE
                    self._write(line)
            # 默认不输出 reasoning 正文；只缓存原始 Markdown，等待 /thinking。
            self.thinking_text += event.text
            return
        # 任何非思考事件：先把本轮折叠状态收束为一行。
        self._end_thinking()
        if isinstance(event, LLMStreamDelta):
            self._finish_live_tool_line()
            if not self._streaming:
                self._write(self._mark(ASSISTANT_MARK))
                self._streaming = True
            self._write(event.text)
        elif isinstance(event, ToolCallStarted):
            self.end_stream()
            record = self._register_tool_call(event.tool_call)
            self._start_live_tool_line(record)
        elif isinstance(event, ToolOutputDelta):
            output_record = self._append_tool_output(event)
            if output_record is not None:
                self._refresh_live_tool_line(output_record)
        elif isinstance(event, ToolCallCompleted):
            self.end_stream()
            self._render_tool_result(event)
        elif isinstance(event, ToolProgress):
            progress_record = self._tool_by_call_id.get(event.tool_call_id)
            if progress_record is not None:
                progress_record.progress = " ".join(event.message.split())
                self._refresh_live_tool_line(progress_record)
        elif isinstance(event, QueryPhaseChanged):
            return
        elif isinstance(event, PlanUpdated):
            return
        elif isinstance(event, UserInputRequested):
            self._finish_live_tool_line()
            self.end_stream()
            self._write(f"  {self._mark(ASSISTANT_MARK)}{event.prompt}\n")
            for index, option in enumerate(event.options, 1):
                self._write(f"    {index}. {option}\n")
            if event.options:
                hint = "输入序号/选项" + (
                    "，或直接输入自定义回答" if event.allow_free_text else "")
                self._write(f"    {hint}\n")
        elif isinstance(event, UserInputResolved):
            return
        elif isinstance(event, PermissionDenied):
            self._finish_live_tool_line()
            self.end_stream()
            self._write(
                f"  {self._mark(RESULT_MARK)}✗ {event.tool_call.name} 被拒绝: {event.reason}\n")
        elif isinstance(event, HookTriggered):
            if event.system_message:
                self._finish_live_tool_line()
                self.end_stream()
                self._write(
                    f"  {self._mark(RESULT_MARK)}hook {event.trigger}: "
                    f"{event.system_message}\n")
        elif isinstance(event, UserSteered):
            self._finish_live_tool_line()
            self.end_stream()
            preview = event.text if len(event.text) <= 60 else event.text[:60] + "…"
            self._write(f"  {self._mark(RESULT_MARK)}插话已注入: {preview}\n")
        elif isinstance(event, SubagentSpawned):
            self._finish_live_tool_line()
            self.end_stream()
            task = event.task if len(event.task) <= 60 else event.task[:60] + "…"
            definition = f" [{event.definition}]" if event.definition else ""
            self._write(
                f"  {self._mark(ASSISTANT_MARK)}agent {event.agent_id}"
                f"{definition} 启动: {task}\n")
        elif isinstance(event, SubagentStateChanged):
            # 终态由 SubagentFinished 单独渲染，避免同一状态打印两遍。
            if event.status in {"paused", "running"}:
                self._finish_live_tool_line()
                self.end_stream()
                reason = f": {event.reason}" if event.reason else ""
                self._write(
                    f"  {self._mark(RESULT_MARK)}agent {event.agent_id} "
                    f"{event.status}{reason}\n")
        elif isinstance(event, SubagentFinished):
            self._finish_live_tool_line()
            self.end_stream()
            detail = f": {event.preview}" if event.preview else ""
            self._write(
                f"  {self._mark(RESULT_MARK)}agent {event.agent_id} "
                f"{event.status}{detail}\n")
        elif isinstance(event, ContextAssembled):
            # 仅由 /context、TUI 面板、recorder 与 SDK 消费，不污染正文。
            return
        elif isinstance(event, ContextMicrocompacted):
            # Microcompaction is visible in panels/JSONL; keep the answer surface clean.
            return
        elif isinstance(event, ContextCompressed):
            self._finish_live_tool_line()
            self.end_stream()
            generation = (
                f" · generation {event.generation}"
                if event.generation else "")
            self._write(
                f"  {self._mark(RESULT_MARK)}历史已压缩: "
                f"{event.before_tokens} → {event.after_tokens} tokens"
                f"{generation}\n")
        elif isinstance(event, ErrorOccurred):
            self._finish_live_tool_line()
            self.end_stream()
            self._write(
                f"  {self._mark(RESULT_MARK)}✗ {event.context}: {event.error}\n")
        elif isinstance(event, RunFinished):
            self._finish_live_tool_line()
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
        live = self._thinking_line_live
        self._thinking_line_live = False
        if not self._thinking_summary_shown and self.thinking_text.strip():
            summary = THINKING_COLLAPSED
            if self._color:
                summary = f"{DIM}{summary}{RESET}"
            if live:
                self._write(REWRITE_LINE + summary + "\n")
            else:
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
        record = self._tool_by_call_id.get(self._tool_key(event.tool_call))
        if record is None:
            record = self._register_tool_call(event.tool_call)
        record.content = self._redact(event.content)
        record.is_error = event.is_error
        record.finished_at = time.monotonic()
        line = self._tool_result_line(record)
        if self._live_tool_key == self._tool_key(event.tool_call):
            self._write(REWRITE_LINE + line + "\n")
            self._live_tool_key = None
        else:
            self._write(line + "\n")
        if self._on_tool_line is not None:
            self._on_tool_line(record.index)

    def _redact(self, text: str) -> str:
        return self._redactor.redact(text) if self._redactor is not None else text

    def _register_tool_call(self, tool_call: ToolCall) -> ToolOutputRecord:
        key = self._tool_key(tool_call)
        existing = self._tool_by_call_id.get(key)
        if existing is not None:
            return existing
        record = ToolOutputRecord(index=len(self.tool_outputs) + 1, tool_call=tool_call)
        self.tool_outputs.append(record)
        self._tool_by_call_id[key] = record
        return record

    @staticmethod
    def _tool_key(tool_call: ToolCall) -> str:
        return tool_call.id or f"object:{id(tool_call)}"

    def _append_tool_output(self, event: ToolOutputDelta) -> Optional[ToolOutputRecord]:
        """缓存增量输出，但绝不自动展开或打印正文。"""
        if not event.text:
            return None
        record = self._tool_by_call_id.get(event.tool_call_id)
        if record is None:
            tool_call = ToolCall(event.tool_call_id, event.tool_name, {})
            record = self._register_tool_call(tool_call)
        record.content += self._redact(event.text)
        lines = [line.strip() for line in record.content.splitlines() if line.strip()]
        if lines:
            record.last_output = _truncate_text(lines[-1], 72)
        return record

    def _tool_status(self, record: ToolOutputRecord) -> str:
        if record.is_error is None:
            symbol = "…"
        else:
            symbol = "✗" if record.is_error else "✓"
        if not self._color:
            return symbol
        color = RED if record.is_error else (CYAN if record.is_error is None else GREEN)
        return f"{color}{symbol}{RESET}"

    def _panel_line(self, text: str) -> None:
        if self._color:
            self._write(f"{TOOL_PANEL_BG}{TOOL_PANEL_FG}{text}")
            padding = max(0, TOOL_PANEL_WIDTH - _visible_width(text))
            if padding:
                if RESET in text:
                    self._write(f"{RESET}{TOOL_PANEL_BG}{TOOL_PANEL_FG}")
                self._write(" " * padding)
            self._write(RESET + "\n")
        else:
            self._write(text + "\n")

    def _find_tool_output(self, selector: str) -> Optional[ToolOutputRecord]:
        if not self.tool_outputs:
            return None
        value = selector.strip().lstrip("#")
        if value in ("", "last", "latest"):
            return self.tool_outputs[-1]
        try:
            index = int(value)
        except ValueError:
            return None
        return next((r for r in self.tool_outputs if r.index == index), None)

    def list_tool_outputs(self) -> bool:
        """列出本轮会话可展开的工具输出。"""
        if not self.tool_outputs:
            return False
        self.end_stream()
        self._write("  工具输出列表（默认折叠）:\n")
        for record in self.tool_outputs:
            status = self._tool_status(record)
            content = record.content
            line_count = len(content.splitlines()) if content else 0
            char_count = len(content)
            state = "已展开" if record.expanded else "已折叠"
            self._write(
                f"    #{record.index} {record.tool_call.name} {status} · "
                f"{line_count} 行 / {char_count} 字符 · {state}\n")
        self._write("  使用 /tool-output <编号> 或 /tool-output last 展开。\n")
        return True

    def expand_tool_output(self, selector: str = "last") -> bool:
        """展开指定工具输出；selector 支持编号、#编号、last。"""
        record = self._find_tool_output(selector)
        if record is None:
            return False
        self.end_stream()
        record.expanded = True
        status = self._tool_status(record)
        self._write(
            f"  {self._mark(RESULT_MARK)}{status} {record.tool_call.name} "
            f"#{record.index} · 工具输出\n")
        header_prefix = f"  ┌─ Tool output #{record.index} · {record.tool_call.name} "
        header = header_prefix + "─" * max(3, TOOL_PANEL_WIDTH - len(header_prefix))
        self._panel_line(header)
        if not record.content.strip():
            content = "（无输出）"
        else:
            content = _clip_preview(
                record.content,
                TOOL_OUTPUT_EXPANDED_LINES,
                TOOL_OUTPUT_EXPANDED_CHARS,
                "工具输出",
            )
        if not record.is_error and record.tool_call.name == "edit_file":
            content = colorize_diff(content, self._color)
        for line in _wrap_tool_content(content):
            self._panel_line(f"  │ {line}")
        self._panel_line("  └" + "─" * (TOOL_PANEL_WIDTH - 3))
        return True

    def clear_tool_outputs(self) -> None:
        self.tool_outputs.clear()
        self._tool_by_call_id.clear()
        self._live_tool_key = None

    @staticmethod
    def _tool_summary(record: ToolOutputRecord) -> str:
        arguments = record.tool_call.arguments
        summary = ""
        for key in ("command", "path", "pattern", "query", "task"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                summary = " ".join(value.split())
                break
        if not summary and arguments:
            summary = " ".join(_format_tool_arguments(arguments).split())
        return _truncate_text(summary, 72)

    @classmethod
    def _tool_goal(cls, record: ToolOutputRecord) -> str:
        summary = cls._tool_summary(record)
        return f"{record.tool_call.name} · {summary}" if summary else record.tool_call.name

    def _tool_running_line(self, record: ToolOutputRecord) -> str:
        details = [self._tool_goal(record)]
        if record.progress:
            details.append(record.progress)
        if record.last_output:
            details.append(record.last_output)
        return (
            f"  {self._mark(ASSISTANT_MARK)}"
            + " · ".join(details)
        )

    def _tool_result_line(self, record: ToolOutputRecord) -> str:
        status = self._tool_status(record)
        line_count = len(record.content.splitlines()) if record.content else 0
        char_count = len(record.content)
        detail = f"{line_count} 行 / {char_count} 字符" if record.content else "无输出"
        elapsed = max(
            0.0,
            (record.finished_at or time.monotonic()) - record.started_at,
        )
        summary = self._tool_summary(record)
        goal = (
            f"{record.tool_call.name} #{record.index} · {summary}"
            if summary else f"{record.tool_call.name} #{record.index}"
        )
        action = (
            f"点击或 /tool-output {record.index} 展开"
            if self._tool_lines_clickable
            else f"/tool-output {record.index} 展开"
        )
        return (
            f"  {self._mark(RESULT_MARK)}{status} "
            f"{goal}"
            f" · {elapsed:.1f}s · {detail} · "
            f"输出已折叠（{action}）"
        )

    def _start_live_tool_line(self, record: ToolOutputRecord) -> None:
        self._finish_live_tool_line()
        self._live_tool_key = self._tool_key(record.tool_call)
        self._write(self._tool_running_line(record))

    def _refresh_live_tool_line(self, record: ToolOutputRecord) -> None:
        if self._live_tool_key != self._tool_key(record.tool_call):
            return
        self._write(REWRITE_LINE + self._tool_running_line(record))

    def _finish_live_tool_line(self) -> None:
        if self._live_tool_key is None:
            return
        record = next(
            (
                item for item in self.tool_outputs
                if self._tool_key(item.tool_call) == self._live_tool_key
            ),
            None,
        )
        if record is not None:
            record.finished_at = record.finished_at or time.monotonic()
            self._write(REWRITE_LINE + self._tool_result_line(record) + "\n")
            if self._on_tool_line is not None:
                self._on_tool_line(record.index)
        else:
            self._write("\n")
        self._live_tool_key = None
