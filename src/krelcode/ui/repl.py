"""REPL：常驻 reader + 状态机（Step 26 重构，支持运行中 steering 与 /stop）。

结构：
- 真实 TTY 由 daemon reader 线程持续读输入 → inbox 队列；测试经 input_fn 注入，零终端
- 主循环用 asyncio.wait 同时盯 inbox 与运行中的 agent 任务：
  · IDLE：处理 /命令；普通消息（含 @文件展开）→ 启动 loop.run 后台任务
  · RUNNING：普通文本 → SessionController.steer（模型在下一 step 看到）；
    /stop 取消并消毒历史；/exit 停止并退出；其他斜杠命令给提示
- 任务自然结束后，队列里未送达的插话会被丢弃并提示（语义可见，不静默吞）
- 最终回复由 StreamRenderer 流式打印（订阅事件总线）
"""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import select
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

try:  # POSIX TTY；Windows 自动退回普通 input()
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None
    tty = None

from .. import __version__
from ..agent.loop import AgentLoop
from ..agent.runtime import AgentRuntime
from ..agent.tokens import estimate_history_tokens
from ..core.events import MemoryWritten, ShellJobFinished
from ..session.commands import (
    CommandRegistry,
    CommandResult,
    builtin_command_registry,
)
from ..session.controller import SessionController
from ..session.input import (
    InputAction,
    InputDelivery,
    InputEnvelope,
    InputKind,
    InputProcessor,
)
from ..session.interactions import UserInputBroker
from ..session.manager import resolve_resume_target
from ..session.memory import (
    SCOPE_LABELS,
    MemoryStore,
    memory_lines,
    parse_memory_note,
    sync_memory_fragment,
)
from ..session.prompt_queue import PromptQueue
from ..session.repl_session import ReplSessionRunner, SessionPrompt
from ..session.state import SessionViewState
from .approval import ApprovalBroker, ApprovalRequest
from .editor import (
    EditBuffer,
    HistorySearch,
    InputHistory,
    sanitize_pasted_text,
)
from .render import CYAN, DIM, RESET, StreamRenderer, auto_color
from .status_line import StatusLineRenderer, read_git_branch
from .tui.controller import PanelController
from .tui.mouse import (
    MOUSE_DISABLE,
    MOUSE_ENABLE,
    parse_cursor_position,
    parse_sgr_mouse,
)
from .tui.panels import build_panels

RUNNING_HINT = (
    "  （任务运行中：Enter=插话，Alt+Enter=排队下一条，Esc/Ctrl-C 中止；"
    "/thinking 展开思考，/plan 查看计划，/tool-output <编号> 展开输出，"
    "/jobs 管理后台任务，/queue 管理队列，/stop 中止，/exit 退出）"
)

IDLE_PROMPT = "❯ "
RUNNING_PROMPT = "↳ "
MENU_ROWS = 8


class ReplControl(str, Enum):
    """终端控制键产生的非文本动作。"""

    INTERRUPT = "interrupt"
    CYCLE_PERMISSION = "cycle-permission"


@dataclass(frozen=True)
class TerminalSubmission:
    """带投递语义的一次文本提交。"""

    text: str
    queue_next: bool = False

_BUILTIN_COMMANDS = builtin_command_registry()
SLASH_COMMANDS = tuple(
    (spec.invocation, spec.description) for spec in _BUILTIN_COMMANDS.list())
RUNNING_SLASH_COMMANDS = tuple(
    (spec.invocation, spec.description)
    for spec in _BUILTIN_COMMANDS.list(running=True))


def match_slash_commands(text: str, *, running: bool = False,
                         limit: int = 9,
                         registry: Optional[CommandRegistry] = None
                         ) -> list[tuple[str, str]]:
    """返回与当前输入前缀匹配的 slash 命令；进入参数后不再弹菜单。"""
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    catalog = registry or _BUILTIN_COMMANDS
    return [(spec.invocation, spec.description)
            for spec in catalog.list(query=text, running=running)[:limit]]


QUEUE_USAGE = "用法：/queue | /queue delete <n> | /queue move <n> <m> | /queue clear"


def _format_job_notification(event: ShellJobFinished) -> str:
    mark = {
        "completed": "✓",
        "failed": "✗",
        "killed": "■",
    }.get(event.status, "•")
    exit_text = f" exit {event.exit_code}" if event.exit_code is not None else ""
    preview = " ".join(event.preview.split())
    if len(preview) > 120:
        preview = preview[:119] + "…"
    suffix = f": {preview}" if preview else ""
    return (
        f"{mark} 后台任务 {event.job_id} {event.status}{exit_text}{suffix}")


def _queue_label(item: Any, limit: int = 100) -> str:
    label = str(getattr(item, "label", "") or "")
    if not label:
        label = str(getattr(item, "raw_text", "") or "")
    if not label:
        label = str(getattr(item, "text", "") or "")
    text = " ".join(label.split())
    return _truncate_display(text, limit)


def _format_prompt_queue(queue: PromptQueue[Any]) -> list[str]:
    if not queue:
        return ["下一条消息队列为空"]
    lines = [f"下一条消息队列（{len(queue)}）:"]
    for index, action in enumerate(queue.entries, 1):
        lines.append(f"  #{index} {_queue_label(action)}")
    return lines


def manage_prompt_queue(
    arguments: str,
    queue: PromptQueue[Any],
    *,
    write: Callable[[str], None] = print,
) -> None:
    """执行 /queue 的纯 UI 管理动作，不进入 SessionController。"""
    parts = arguments.split()
    if not parts:
        for line in _format_prompt_queue(queue):
            write(line)
        return
    operation = parts[0].lower()
    try:
        if operation == "clear" and len(parts) == 1:
            count = len(queue)
            queue.clear()
            write(f"已清空下一条消息队列（{count} 条）")
            return
        if operation == "delete" and len(parts) == 2:
            action = queue.delete(int(parts[1]))
            write(f"已从队列删除: {_queue_label(action)}")
            return
        if operation == "move" and len(parts) == 3:
            source = int(parts[1])
            target = int(parts[2])
            queue.move(source, target)
            write(f"已把队列 #{source} 移动到 #{target}")
            return
    except (ValueError, IndexError) as exc:
        message = str(exc) if str(exc) else "序号必须是整数"
        write(f"队列操作失败: {message}")
        return
    write(QUEUE_USAGE)


def format_plan_lines(state: SessionViewState) -> list[str]:
    """把 reducer 中的计划快照渲染为纯文本行，供 /plan 与测试复用。"""
    if not state.plan_items:
        return ["当前没有执行计划"]
    statuses = list(state.plan_statuses)
    if len(statuses) != len(state.plan_items):
        statuses = [
            "in_progress" if index == state.plan_active_index else "pending"
            for index in range(len(state.plan_items))
        ]
    marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    completed = sum(status == "completed" for status in statuses)
    lines = [f"当前执行计划（{completed}/{len(statuses)} 完成）:"]
    for index, (item, status) in enumerate(
            zip(state.plan_items, statuses), 1):
        lines.append(f"  {marks.get(status, '[?]')} {index}. {item}")
    if state.plan_explanation:
        lines.append(f"  说明: {state.plan_explanation}")
    approval = {
        "pending": "待确认 · /plan approve 执行 · /plan cancel 取消",
        "approved": "已批准",
        "cancelled": "已取消",
    }.get(state.plan_approval_status)
    if approval:
        lines.append(f"  状态: {approval}")
    return lines


def handle_plan_command(
    arguments: str,
    plan_manager,
    state: SessionViewState,
    *,
    allow_actions: bool = True,
) -> CommandResult:
    """执行 `/plan` 的查看、生成、批准与取消动作。"""
    text = (arguments or "").strip()
    operation = text.lower()
    if operation == "approve":
        if not allow_actions:
            return CommandResult(
                handled=True,
                output=("  计划执行确认需等待当前任务结束后操作",),
            )
        ok, message, prompt = plan_manager.approve()
        state.plan_approval_status = plan_manager.status.value
        return CommandResult(
            handled=True,
            output=(f"  [plan] {message}",),
            run_prompt=prompt if ok else None,
        )
    if operation == "cancel":
        if not allow_actions:
            return CommandResult(
                handled=True,
                output=("  计划取消需等待当前任务结束后操作",),
            )
        ok, message = plan_manager.cancel()
        state.plan_approval_status = plan_manager.status.value
        return CommandResult(handled=True, output=(f"  [plan] {message}",))
    if text:
        if not allow_actions:
            return CommandResult(
                handled=True,
                output=("  当前任务运行中：只能使用 /plan 查看计划",),
            )
        try:
            prompt = plan_manager.start_request(text)
        except ValueError as exc:
            return CommandResult(
                handled=True, output=(f"  [plan] {exc}",))
        state.plan_approval_status = plan_manager.status.value
        return CommandResult(
            handled=True,
            output=("  [plan] 已进入 Plan 模式，开始只读调查并生成计划",),
            run_prompt=prompt,
        )
    lines = (
        plan_manager.render_lines()
        if plan_manager.snapshot.items
        else format_plan_lines(state)
    )
    return CommandResult(
        handled=True,
        output=tuple(f"  {line}" for line in lines),
    )


def _stdout_write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 宽字符按 2 列计算，ANSI 控制序列按 0 计算。"""
    plain = _ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in plain)


def _truncate_display(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    out = []
    used = 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + char_width > width - 1:
            break
        out.append(ch)
        used += char_width
    return "".join(out) + "…"


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _input_rule(color: bool = False) -> str:
    """pi 风格输入区上边界；留 1 列避免终端自动换行产生空行。"""
    columns = shutil.get_terminal_size((80, 24)).columns
    rule = "─" * max(1, columns - 1)
    return f"{DIM}{rule}{RESET}" if color else rule


class PromptWriter:
    """多行输入框渲染器；每次从顶边界整体重绘，保证光标和菜单一致。"""

    CLEAR_LINE = "\r\x1b[2K"
    CLEAR_BELOW = "\x1b[0J"
    CURSOR_UP = "\x1b[1A"
    CURSOR_DOWN = "\x1b[1B"

    def __init__(self, write: Optional[Callable[[str], None]] = None,
                 enabled: Optional[bool] = None,
                 color: Optional[bool] = None,
                 *, cursor_probe: bool = False) -> None:
        self._write = write or _stdout_write
        self.enabled = sys.stdout.isatty() if enabled is None else enabled
        self._color = auto_color() if color is None else color
        self._visible = False
        self._last = ""
        self._prompt_width = 0
        self._suggestions: tuple[str, ...] = ()
        self._input_rows = 1
        self._rendered_rows = 0
        self._cursor_row = 0
        self._submitted = False
        self._status = ""
        self._last_status = ""
        self._panel_lines: tuple[str, ...] = ()
        self._cursor_probe = cursor_probe
        self._lock = threading.RLock()

    @property
    def _rule(self) -> str:
        """当前终端宽度下的输入区边界，供渲染诊断与测试使用。"""
        return _input_rule(self._color)

    def show(self, text: str) -> None:
        with self._lock:
            if not self.enabled:
                return
            if self._visible:
                if (self._last and self._last[0] == text
                        and not self._last[1]
                        and self._last_status == self._status):
                    self._submitted = False
                    return
                self.hide()
            row, column = self._end_position(text)
            self._render(text, [], cursor_row=row, cursor_column=column)
            self._submitted = False

    def update(
        self,
        text: str,
        suggestions: Optional[list[str]] = None,
        *,
        cursor_row: Optional[int] = None,
        cursor_column: Optional[int] = None,
    ) -> None:
        """重绘任意多行输入；cursor_row 以输入正文第一行为 0。"""
        with self._lock:
            if not self.enabled or not self._visible:
                return
            lines = text.split("\n")
            row = (len(lines) - 1 if cursor_row is None
                   else max(0, min(cursor_row, len(lines) - 1)))
            column = (_display_width(lines[row]) if cursor_column is None
                      else max(0, cursor_column))
            signature = (
                text, tuple(suggestions or ()), row, column, self._status)
            if signature == (*self._last, self._last_status):
                return
            self._render(text, suggestions or [], row, column)

    def set_panels(self, lines: list[str] | tuple[str, ...],
                   *, redraw: bool = True) -> None:
        """Update the fixed-height panel dock above the input editor."""
        with self._lock:
            normalized = tuple(lines)
            if normalized == self._panel_lines:
                return
            self._panel_lines = normalized
            if not redraw or not self.enabled or not self._visible or not self._last:
                return
            text, suggestions, row, column = self._last
            self._render(text, list(suggestions), row, column)

    def set_cursor_probe(self, enabled: bool) -> None:
        self._cursor_probe = bool(enabled)

    @property
    def cursor_row_relative(self) -> int:
        return self._cursor_row

    def set_status(self, status: str) -> None:
        """更新输入框下方的单行状态；可见时原地重绘且保留草稿。"""
        with self._lock:
            status = " ".join((status or "").split())
            if status == self._status:
                return
            self._status = status
            if not self.enabled or not self._visible or not self._last:
                return
            text, suggestions, row, column = self._last
            self._render(text, list(suggestions), row, column)

    def suspend(self) -> None:
        """临时让出终端输出区，但保留可见草稿以便 ``resume``。"""
        with self._lock:
            if not self.enabled or not self._visible:
                return
            self._move_to_top()
            self._write(self.CLEAR_BELOW)
            self._visible = False

    def resume_or_show(self, fallback: str) -> None:
        """恢复 suspend 前草稿；没有草稿时显示 fallback 提示符。"""
        with self._lock:
            if not self.enabled or self._visible:
                return
            if self._last:
                text, suggestions, row, column = self._last
                self._render(text, list(suggestions), row, column)
            else:
                self.show(fallback)

    def retarget_prompt(self, old: str, new: str) -> None:
        """切换运行/空闲提示符，同时保留用户已经输入的草稿。"""
        with self._lock:
            if not self.enabled or not self._last:
                return
            text, suggestions, row, column = self._last
            if not text.startswith(old):
                return
            self._last = (
                new + text[len(old):], suggestions, row, column)
            if self._visible:
                text, suggestions, row, column = self._last
                self._render(text, list(suggestions), row, column)

    @staticmethod
    def _end_position(text: str) -> tuple[int, int]:
        lines = text.split("\n")
        return len(lines) - 1, _display_width(lines[-1])

    def _block(self, text: str, suggestions: list[str]) -> list[str]:
        rule = _input_rule(self._color)
        input_lines = text.split("\n")
        status_rows: list[str] = []
        if self._status:
            columns = shutil.get_terminal_size((80, 24)).columns
            status = _truncate_display(
                f"  {self._status}", max(1, columns - 1))
            status_rows.append(
                f"{DIM}{status}{RESET}" if self._color else status)
        menu_capacity = max(0, MENU_ROWS - len(status_rows))
        menu = list(suggestions[:menu_capacity])
        menu.extend([""] * (menu_capacity - len(menu)))
        self._input_rows = len(input_lines)
        return [*self._panel_lines, rule, *input_lines, rule, *status_rows, *menu]

    def _move_to_top(self) -> None:
        if self._cursor_row > 0:
            self._write("\r" + self.CURSOR_UP * self._cursor_row)
        else:
            self._write("\r")

    def _render(
        self,
        text: str,
        suggestions: list[str],
        cursor_row: int,
        cursor_column: int,
    ) -> None:
        if self._visible:
            self._move_to_top()
            self._write(self.CLEAR_BELOW)
        rows = self._block(text, suggestions)
        self._write("\n".join(rows))
        self._rendered_rows = len(rows)
        target_row = len(self._panel_lines) + 1 + cursor_row
        moves_up = self._rendered_rows - 1 - target_row
        self._write("\r")
        if moves_up > 0:
            self._write(self.CURSOR_UP * moves_up)
        if cursor_column > 0:
            self._write(f"\x1b[{cursor_column}C")
        if self._cursor_probe and self.enabled:
            self._write("\x1b[6n")
        self._visible = True
        self._cursor_row = target_row
        self._prompt_width = cursor_column
        self._suggestions = tuple(suggestions[:MENU_ROWS])
        self._last = (text, tuple(suggestions), cursor_row, cursor_column)
        self._last_status = self._status

    def submitted(self, *, raw: bool = True) -> None:
        """保留输入框，清掉菜单区，把输出光标放到下边界之后。"""
        with self._lock:
            was_visible = self.enabled and self._visible
            if was_visible:
                self._move_to_top()
                rows_down = max(0, self._rendered_rows - 1 - self._cursor_row)
                self._write("\r")
                if rows_down > 0:
                    self._write(self.CURSOR_DOWN * rows_down)
                self._write(self.CLEAR_BELOW)
            self._visible = False
            self._last = ""
            self._last_status = ""
            self._submitted = was_visible

    def hide(self) -> None:
        with self._lock:
            if not self.enabled or not self._visible:
                return
            self._move_to_top()
            self._write(self.CLEAR_BELOW)
            self._visible = False
            self._last = ""
            self._last_status = ""
            self._cursor_row = 0

    def finish(self) -> None:
        """退出前结束当前终端行，不在 shell 留下孤立空行。"""
        with self._lock:
            if not self.enabled:
                return
            if self._visible:
                self.hide()
            if not self._submitted:
                self._write("\r\n")
            self._submitted = False


class TerminalLineEditor:
    """零依赖字符级编辑器：多行编辑、历史搜索、粘贴和 slash 候选。"""

    BRACKETED_PASTE_ON = "\x1b[?2004h"
    BRACKETED_PASTE_OFF = "\x1b[?2004l"
    PASTE_END = b"\x1b[201~"
    PASTE_LIMIT = 1_000_000
    COMPLETE_ESCAPE_SEQUENCES = frozenset({
        b"[A", b"OA", b"[B", b"OB", b"[D", b"OD", b"[C", b"OC",
        b"[H", b"OH", b"[1~", b"[7~", b"[F", b"OF", b"[4~", b"[8~",
        b"[3~", b"[3;5~", b"[200~", b"[Z", b"[13;2u", b"[13;3u",
        b"[27;2;13~", b"[27;3;13~",
        b"b", b"B", b"f", b"F", b"\x7f", b"\r", b"\n",
    })

    def __init__(self, prompt: PromptWriter,
                 suggest: Callable[[str], list[tuple[str, str]]],
                 color: bool = False,
                 history: Optional[InputHistory] = None,
                 panels: Optional[PanelController] = None) -> None:
        self._prompt = prompt
        self._suggest = suggest
        self._color = color
        self._history = history if history is not None else InputHistory()
        self._panels = panels
        self._pending_input = bytearray()
        if panels is not None:
            prompt.set_cursor_probe(True)

    @property
    def available(self) -> bool:
        return bool(termios and tty and sys.stdin.isatty())

    def readline(
        self, prompt_text: Callable[[], str],
    ) -> str | TerminalSubmission | ReplControl:
        if not self.available:
            return input("")
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        # 保留输出处理（\n 正常换行），但不让 Ctrl-C 绕过 finally 恢复终端。
        editor_settings = termios.tcgetattr(fd)
        # Enter 保持 CR，Ctrl-J 才是 LF；否则终端 ICRNL 后两者无法区分，
        # 会把普通 Enter 错当成多行插入。
        editor_settings[0] &= ~getattr(termios, "ICRNL", 0)
        editor_settings[3] &= ~termios.ISIG
        # Ctrl-O is the panel-focus key. In cbreak mode the terminal driver may
        # otherwise interpret it as VDISCARD and silently drop it.
        if hasattr(termios, "VDISCARD"):
            editor_settings[6][termios.VDISCARD] = b"\x00"
        termios.tcsetattr(fd, termios.TCSADRAIN, editor_settings)
        self._set_bracketed_paste(fd, True)
        if self._panels is not None:
            self._set_mouse_reporting(fd, True)
        buffer = EditBuffer()
        search: Optional[HistorySearch] = None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._history.reset_navigation()
        try:
            while True:
                data = self._read_one(fd)
                if not data:
                    raise EOFError
                if data == b"\x1b":
                    sequence = self._read_escape_sequence(fd)
                    if sequence == b"[200~":
                        pasted = self._read_bracketed_paste(fd)
                        if search is not None:
                            search.query += pasted.replace("\n", " ")
                            self._run_history_search(search, buffer)
                        else:
                            buffer.insert(pasted)
                        self._refresh(prompt_text(), buffer, search=search)
                        continue
                    if self._panels is not None:
                        cursor = parse_cursor_position(sequence)
                        if cursor is not None:
                            self._panels.set_cursor_origin(
                                cursor[0], self._prompt.cursor_row_relative)
                            self._refresh(prompt_text(), buffer, search=search)
                            continue
                        if parse_sgr_mouse(sequence) is not None:
                            self._panels.handle_mouse(sequence)
                            self._refresh(prompt_text(), buffer, search=search)
                            continue
                        panel_key = self._panels.key_from_escape(sequence)
                        if self._panels.handle_key(panel_key):
                            self._refresh(prompt_text(), buffer, search=search)
                            continue
                    if search is not None:
                        if sequence in (b"", b"G"):
                            search.cancel(buffer)
                            search = None
                        elif sequence in (b"\r", b"\n"):
                            search = None
                        else:
                            escaped = self._apply_escape_sequence(sequence, buffer)
                            if escaped is not None:
                                return escaped
                            search = None
                        self._refresh(prompt_text(), buffer)
                        continue
                    escaped = self._apply_escape_sequence(sequence, buffer)
                    if escaped is not None:
                        return escaped
                    self._navigate_history(sequence, buffer)
                    self._refresh(prompt_text(), buffer)
                    continue
                char = decoder.decode(data)
                if not char:
                    continue
                if search is not None:
                    if char == "\x12":            # Ctrl-R：继续向更旧匹配
                        search.cycle(self._history)
                        if search.matched:
                            buffer.set_text(search.match_text)
                    elif char in ("\x07", "\x03"):
                        if char == "\x03":
                            return ReplControl.INTERRUPT
                        search.cancel(buffer)
                        search = None
                    elif char in ("\x7f", "\x08"):
                        if search.query:
                            search.query = search.query[:-1]
                            self._run_history_search(search, buffer)
                    elif char == "\r":
                        search = None          # Enter 只接受匹配，不立刻提交
                    elif char >= " " and char != "\x7f":
                        search.query += char
                        self._run_history_search(search, buffer)
                    self._refresh(prompt_text(), buffer, search=search)
                    continue
                if (self._panels is not None and self._panels.focused
                        and char not in ("\x03", "\x0f", "\t")):
                    # Panel focus is modal: Enter returns to the editor and all
                    # other editing keys are consumed instead of mutating text.
                    self._panels.handle_key(
                        "enter" if char == "\r" else "")
                    self._refresh(prompt_text(), buffer, search=search)
                    continue
                if char == "\r":
                    return TerminalSubmission(buffer.text)
                if char == "\n":               # Ctrl-J：插入换行
                    buffer.insert("\n")
                if char in ("\x7f", "\x08"):
                    buffer.backspace()
                elif char == "\x04":             # Ctrl-D
                    if not buffer.text:
                        raise EOFError
                    buffer.delete()
                elif char == "\x03":             # Ctrl-C（通常由 ISIG 先行处理）
                    return ReplControl.INTERRUPT
                elif char == "\x01":             # Ctrl-A
                    buffer.move_home()
                elif char == "\x05":             # Ctrl-E
                    buffer.move_end()
                elif char == "\x02":             # Ctrl-B
                    buffer.move_left()
                elif char == "\x06":             # Ctrl-F
                    buffer.move_right()
                elif char == "\x0b":             # Ctrl-K
                    buffer.delete_to_end()
                elif char == "\x15":             # Ctrl-U
                    buffer.delete_to_start()
                elif char == "\x17":             # Ctrl-W
                    buffer.delete_word_before()
                elif char == "\x10":             # Ctrl-P
                    self._navigate_history(b"[A", buffer)
                elif char == "\x0e":             # Ctrl-N
                    self._navigate_history(b"[B", buffer)
                elif char == "\x12":             # Ctrl-R
                    search = HistorySearch.begin(buffer)
                    self._run_history_search(search, buffer)
                elif char == "\x0f":             # Ctrl-O
                    if self._panels is not None:
                        self._panels.handle_key("ctrl-o")
                elif char == "\t":
                    if (self._panels is not None
                            and self._panels.handle_key("tab")):
                        pass
                    else:
                        self._complete(buffer)
                elif char >= " " and char != "\x7f":
                    buffer.insert(char)
                self._refresh(prompt_text(), buffer, search=search)
        finally:
            if self._panels is not None:
                self._set_mouse_reporting(fd, False)
            self._set_bracketed_paste(fd, False)
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _read_one(self, fd: int) -> bytes:
        if self._pending_input:
            data = bytes(self._pending_input[:1])
            del self._pending_input[:1]
            return data
        return os.read(fd, 1)

    def _read_escape_sequence(self, fd: int) -> bytes:
        """读取 ESC 后紧跟的控制序列；其他终端转义自然忽略。"""
        sequence = bytearray()
        while len(sequence) < 32:
            if not self._pending_input and not select.select(
                [fd], [], [], 0.01
            )[0]:
                break
            sequence.extend(self._read_one(fd))
            raw = bytes(sequence)
            if raw in TerminalLineEditor.COMPLETE_ESCAPE_SEQUENCES:
                break
            if parse_cursor_position(raw) is not None:
                break
            if raw.startswith(b"[<") and raw[-1:] in (b"M", b"m"):
                break
        return bytes(sequence)

    def _read_bracketed_paste(self, fd: int) -> str:
        """读取到 Bracketed paste 结束标记，并返回净化后的文本。"""
        payload = bytearray()
        carry = b""
        limit_reached = False

        def append_limited(segment: bytes) -> None:
            nonlocal limit_reached
            remaining = self.PASTE_LIMIT - len(payload)
            if remaining > 0:
                payload.extend(segment[:remaining])
            if len(segment) > max(0, remaining):
                limit_reached = True

        while True:
            if self._pending_input:
                chunk = bytes(self._pending_input)
                self._pending_input.clear()
            else:
                ready = select.select([fd], [], [], 10.0)[0]
                if not ready:
                    append_limited(carry)
                    break
                chunk = os.read(fd, 4096)
            if not chunk:
                append_limited(carry)
                break

            data = carry + chunk
            marker_index = data.find(self.PASTE_END)
            if marker_index >= 0:
                self._pending_input = bytearray(
                    data[marker_index + len(self.PASTE_END):])
                append_limited(data[:marker_index])
                break
            safe_length = max(0, len(data) - len(self.PASTE_END) + 1)
            append_limited(data[:safe_length])
            carry = data[safe_length:]
        text = payload.decode("utf-8", errors="replace")
        text = sanitize_pasted_text(text)
        if limit_reached:
            text += "\n[粘贴内容过大，已截断]"
        return text

    @staticmethod
    def _set_mouse_reporting(fd: int, enabled: bool) -> None:
        try:
            os.write(fd, (MOUSE_ENABLE if enabled else MOUSE_DISABLE).encode("ascii"))
        except OSError:
            pass

    @staticmethod
    def _set_bracketed_paste(fd: int, enabled: bool) -> None:
        sequence = (TerminalLineEditor.BRACKETED_PASTE_ON if enabled
                    else TerminalLineEditor.BRACKETED_PASTE_OFF)
        try:
            os.write(fd, sequence.encode("ascii"))
        except OSError:
            pass

    def _run_history_search(
        self,
        search: HistorySearch,
        buffer: EditBuffer,
    ) -> bool:
        search.match_index = None
        matched = search.find(self._history)
        if matched:
            buffer.set_text(search.match_text)
        return matched

    @staticmethod
    def _apply_escape_sequence(
        sequence: bytes,
        buffer: EditBuffer | list[str],
    ) -> Optional[TerminalSubmission | ReplControl]:
        """应用光标/删除类转义键；特殊提交动作直接返回。"""
        legacy_list = isinstance(buffer, list)
        state = EditBuffer("".join(buffer)) if legacy_list else buffer

        def finish() -> None:
            if legacy_list:
                buffer[:] = list(state.text)

        if not sequence:
            return ReplControl.INTERRUPT       # 单独 ESC
        if sequence == b"[Z":                  # Shift-Tab
            return ReplControl.CYCLE_PERMISSION
        if sequence in (b"\r", b"\n", b"[13;3u", b"[27;3;13~"):
            return TerminalSubmission(state.text, queue_next=True)  # Alt+Enter
        if sequence in (b"[13;2u", b"[27;2;13~"):  # Shift+Enter
            state.insert("\n")
        elif sequence in (b"[D", b"OD"):       # Left
            state.move_left()
        elif sequence in (b"[C", b"OC"):       # Right
            state.move_right()
        elif sequence in (b"[H", b"OH", b"[1~", b"[7~"):  # Home
            state.move_home()
        elif sequence in (b"[F", b"OF", b"[4~", b"[8~"):  # End
            state.move_end()
        elif sequence == b"[3~":               # Delete
            state.delete()
        elif sequence == b"[3;5~":             # Ctrl-Delete
            state.delete_word_after()
        elif sequence in (b"b", b"B"):         # Alt-B
            state.move_word_left()
        elif sequence in (b"f", b"F"):         # Alt-F
            state.move_word_right()
        elif sequence == b"\x7f":              # Alt-Backspace
            state.delete_word_before()
        finish()
        return None

    def _navigate_history(
        self, sequence: bytes, buffer: EditBuffer | list[str]
    ) -> bool:
        """应用 ↑/↓；多行文本内优先移动光标行。"""
        legacy_list = isinstance(buffer, list)
        state = EditBuffer("".join(buffer)) if legacy_list else buffer

        def finish() -> None:
            if legacy_list:
                buffer[:] = list(state.text)

        if sequence in (b"[A", b"OA"):
            if "\n" in state.text and state.move_line_up():
                finish()
                return True
            state.set_text(self._history.previous(state.text))
            finish()
            return True
        if sequence in (b"[B", b"OB"):
            if "\n" in state.text and state.move_line_down():
                finish()
                return True
            state.set_text(self._history.next(state.text))
            finish()
            return True
        return False

    def _complete(self, buffer: EditBuffer | list[str]) -> None:
        legacy_list = isinstance(buffer, list)
        state = EditBuffer("".join(buffer)) if legacy_list else buffer
        matches = [name for name, _ in self._suggest(state.text)]
        if not matches:
            return
        if len(matches) == 1:
            state.set_text(matches[0] + " ")
        else:
            common = os.path.commonprefix(matches)
            if len(common) > len(state.text):
                state.set_text(common)
        if legacy_list:
            buffer[:] = list(state.text)

    def _refresh(
        self,
        prompt_text: str,
        buffer: EditBuffer | list[str],
        *,
        search: Optional[HistorySearch] = None,
    ) -> None:
        state = EditBuffer("".join(buffer)) if isinstance(buffer, list) else buffer
        source_lines = state.text.split("\n")
        indent = " " * _display_width(prompt_text)
        rendered_lines = [prompt_text + source_lines[0]]
        rendered_lines.extend(indent + line for line in source_lines[1:])

        matches = [] if "\n" in state.text else self._suggest(state.text)
        columns = shutil.get_terminal_size((80, 24)).columns
        lines: list[str] = []
        if search is not None:
            match = search.match_text if search.matched else "无匹配"
            status = f"  (reverse-i-search) `{search.query}`: {match}"
            if self._color:
                status = f"{DIM}{status}{RESET}"
            lines.append(_truncate_display(status, max(1, columns - 1)))
        for name, description in matches:
            label = f"{name:<11}"
            line = _truncate_display(
                f"{indent}{label}{description}", max(1, columns - 1))
            if self._color:
                prefix = f"{indent}{CYAN}{name:<11}{RESET}"
                line = prefix + line[len(f"{indent}{label}"):]
            lines.append(line)
        cursor_line, _ = state.cursor_line_column()
        line_start = state.text.rfind("\n", 0, state.cursor) + 1
        cursor_text = state.text[line_start:state.cursor]
        cursor_column = _display_width(cursor_text)
        if cursor_line == 0:
            cursor_column += _display_width(prompt_text)
        if self._panels is not None:
            layout = self._panels.render(max(1, columns - 1))
            self._prompt.set_panels(layout.lines, redraw=False)
        self._prompt.update(
            "\n".join(rendered_lines),
            lines,
            cursor_row=cursor_line,
            cursor_column=cursor_column,
        )


def print_banner(loop: AgentLoop, tool_names: Optional[list],
                 session_path: Optional[str] = None,
                 write: Callable[[str], None] = print,
                 color: Optional[bool] = None) -> None:
    """启动信息卡片：模型、工作区、工具与会话，保持主流 coding agent 风格。"""
    if color is None:
        color = auto_color()
    provider = getattr(loop.llm, "name", "?")
    model = getattr(loop.llm, "model", "?")
    workspace = loop.tool_context.workspace_root
    columns = shutil.get_terminal_size((80, 24)).columns
    inner = max(36, min(columns - 4, 96))
    title = f"✻ Krelcode v{__version__}"
    title_rendered = f"{CYAN}{title}{RESET}" if color else title
    fill = max(0, inner - _display_width(title) - 3)

    write(f"  ╭─ {title_rendered} " + "─" * fill + "╮")

    def row(label: str, value: str, path: bool = False) -> None:
        available = inner - 14
        if path and _display_width(value) > available:
            value = "…" + value[-(available - 1):]
        else:
            value = _truncate_display(value, available)
        label_text = f"{label:<10}"
        rendered_label = f"{DIM}{label_text}{RESET}" if color else label_text
        write(f"  │ {_pad_display(rendered_label + value, inner - 2)} │")

    row("provider", f"{provider} · {model}")
    row("workspace", str(workspace), path=True)
    row("tools", f"{len(tool_names or [])} 个 · /tools 查看")
    if session_path:
        row("session", session_path, path=True)
    write("  ╰" + "─" * inner + "╯")

    accent = (lambda s: f"{CYAN}{s}{RESET}") if color else (lambda s: s)
    write("")
    write(f"  {accent('/help')} 查看命令 · Shift-Tab 切换权限 · Ctrl-O 面板 · "
          f"↑/↓ 历史 · Ctrl-R 搜索 · {accent('@路径')} 引用文件 · "
          f"{accent('!命令')} Shell · {accent('#记忆')} 记忆 · {accent('/exit')} 退出")
    write("")


async def run_repl(
    loop: AgentLoop,
    tool_names: Optional[list] = None,
    session_path: Optional[str] = None,
    sessions_dir: Optional[Path] = None,
    mcp_supervisor=None,
    mcp_reload=None,
    subagents=None,
    checkpoints=None,
    usage=None,
    model_switch=None,
    skills=None,
    skills_reload=None,
    plugins=None,
    plugins_reload=None,
    input_fn=None,
    redactor=None,
    runtime: Optional[AgentRuntime] = None,
    input_processor: Optional[InputProcessor] = None,
    approval_broker: Optional[ApprovalBroker] = None,
    input_broker: Optional[UserInputBroker] = None,
    history_path: Optional[Path] = None,
    persist_history: Optional[bool] = None,
) -> int:
    if input_broker is not None and loop.user_input_port is None:
        # 直接嵌入 run_repl 的调用方可能先构建 AgentLoop；这里补齐端口。
        loop.user_input_port = input_broker
    runtime = runtime or AgentRuntime(
        loop,
        subagents=subagents,
        mcp_supervisor=mcp_supervisor,
        mcp_reload=mcp_reload,
    )
    loop = runtime.loop
    use_color = auto_color()
    prompt = PromptWriter(color=use_color)

    def _prompt_write(s: str) -> None:
        # 输出让出输入区但保留草稿；事件消费完立即由状态栏恢复光标。
        prompt.suspend()
        _stdout_write(s)

    renderer = StreamRenderer(write=_prompt_write, redactor=redactor)
    explicit_history_path = history_path is not None
    persistent_history = (
        (input_fn is None or explicit_history_path)
        if persist_history is None else persist_history
    )
    if history_path is None:
        configured_history = os.environ.get("KRELCODE_HISTORY_FILE")
        history_path = (Path(configured_history).expanduser()
                        if configured_history
                        else Path.home() / ".krelcode" / "history.jsonl")
    history_redactor = getattr(redactor, "redact", None)
    input_history = InputHistory(
        path=history_path if persistent_history else None,
        redact=history_redactor,
    )
    workspace_root = loop.tool_context.workspace_root
    processor = input_processor or InputProcessor(workspace_root)
    controller = SessionController(
        runtime,
        session_path=session_path,
        sessions_dir=sessions_dir,
        mcp_supervisor=mcp_supervisor,
        mcp_reload=mcp_reload,
        subagents=subagents,
        checkpoints=checkpoints,
        usage=usage,
        model_switch=model_switch,
        skills=skills,
        skills_reload=skills_reload,
        plugins=plugins,
        plugins_reload=plugins_reload,
        workspace_root=workspace_root,
        command_registry=processor.command_registry,
    )

    memory_store = getattr(loop, "memory_store", None)
    if memory_store is None:
        memory_store = MemoryStore(workspace_root, redactor=redactor)
        loop.memory_store = memory_store
    else:
        memory_store.redactor = redactor
    sync_memory_fragment(loop, memory_store)

    state = SessionViewState()
    panel_controller = PanelController()
    quick_status = ""

    # Panel redraws are high-frequency and must stay disk-free. Refresh this
    # cache only after an explicit memory mutation or MemoryWritten event.
    memory_cache = memory_store.dashboard_snapshot(limit=8)

    def refresh_memory_cache() -> None:
        nonlocal memory_cache
        memory_cache = memory_store.dashboard_snapshot(limit=8)

    def refresh_panels(*, queue_size: int = 0) -> None:
        snapshot = getattr(loop, "prompt_snapshot", None)
        fragments = getattr(snapshot, "fragments", ())
        cache = getattr(snapshot, "cache", None)
        context_manager = getattr(loop, "context_manager", None)
        columns = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
        panels = build_panels(
            state,
            permission_mode=controller.permission_manager.mode.value,
            queue_size=queue_size,
            context_tokens=estimate_history_tokens(loop.history),
            context_budget=getattr(context_manager, "max_tokens", None),
            fragment_count=len(fragments),
            stable_fragments=len(getattr(cache, "stable_fragments", ())),
            memory_entries=memory_cache.entries,
            memory_total=memory_cache.total_entries,
            memory_tokens=memory_cache.tokens,
            quick_status=quick_status,
        )
        panel_controller.set_panels(panels)
        layout = panel_controller.render(columns)
        prompt.set_panels(layout.lines)

    print_banner(loop, tool_names, session_path=session_path)
    status_renderer = getattr(loop, "status_line_renderer", None)
    if not isinstance(status_renderer, StatusLineRenderer):
        status_renderer = StatusLineRenderer()
    status_branch = read_git_branch(workspace_root)
    raw_inbox: asyncio.Queue = asyncio.Queue()
    action_queue: asyncio.Queue = asyncio.Queue()
    job_notifications: asyncio.Queue = asyncio.Queue()

    def on_job_finished(event) -> None:
        if isinstance(event, ShellJobFinished):
            job_notifications.put_nowait(event)

    unsubscribe_job_notifications = loop.bus.subscribe(on_job_finished)
    input_ready = threading.Event()
    status_cache = ""
    last_status_draw = 0.0

    def styled_prompt(running: bool) -> str:
        text = RUNNING_PROMPT if running else IDLE_PROMPT
        return f"{CYAN}{text}{RESET}" if use_color else text

    def current_prompt() -> str:
        return styled_prompt(state.is_running)

    def suggest(text: str) -> list[tuple[str, str]]:
        return match_slash_commands(
            text, running=state.is_running, registry=processor.command_registry)

    editor = TerminalLineEditor(
        prompt, suggest, color=use_color, history=input_history,
        panels=panel_controller)

    def render_status(view: SessionViewState) -> str:
        """Build a status line without touching disk on every redraw."""
        progress = view.status_text()
        if not progress and controller.plan_manager.pending:
            progress = "计划待确认 · /plan approve 执行"
        tokens = estimate_history_tokens(loop.history)
        manager = getattr(loop, "context_manager", None)
        budget = getattr(manager, "max_tokens", None)
        context = f"ctx {tokens}/{budget}" if budget else f"ctx {tokens}"
        provider = str(getattr(loop.llm, "name", "") or "")
        model = str(getattr(loop.llm, "model", "") or "")
        session = (
            Path(session_path).stem if session_path else "")
        return status_renderer.render(
            provider=provider,
            model=model,
            permission=(
                f"权限 {controller.permission_manager.mode.value}"),
            session=session,
            cwd=str(workspace_root),
            status=progress,
            tokens=str(tokens),
            context=context,
            branch=status_branch,
        )

    def refresh_status(
        current: Optional[SessionViewState] = None,
        *,
        force: bool = False,
    ) -> None:
        nonlocal status_cache, last_status_draw
        refresh_panels(queue_size=len(runner.queue))
        now = time.monotonic()
        if not force and now - last_status_draw < 0.15:
            return
        view = current or state
        status = render_status(view)
        if status != status_cache:
            status_cache = status
            prompt.set_status(status)
        if view.is_running:
            prompt.retarget_prompt(
                styled_prompt(False), styled_prompt(True))
            prompt.resume_or_show(current_prompt())
        else:
            prompt.retarget_prompt(
                styled_prompt(True), styled_prompt(False))
        last_status_draw = now

    def restore_prompt() -> None:
        nonlocal status_cache, last_status_draw
        refresh_panels(queue_size=len(runner.queue))
        status_cache = render_status(state)
        prompt.set_status(status_cache)
        prompt.resume_or_show(current_prompt())
        last_status_draw = time.monotonic()
        input_ready.set()

    def notice(message: str) -> None:
        prompt.suspend()
        _stdout_write(f"  {message}\n")
        if state.is_running:
            refresh_status(state, force=True)

    def redact_value(value) -> str:
        text = str(value)
        return redactor.redact(text) if redactor is not None else text

    def show_approval(request: ApprovalRequest) -> None:
        tool_call = request.tool_call
        command = tool_call.arguments.get("command")
        detail = command if command is not None else tool_call.arguments
        prompt.suspend()
        _stdout_write(f"  [审批] {tool_call.name}: {redact_value(detail)}\n")
        _stdout_write(f"  原因: {redact_value(request.reason)}\n")
        _stdout_write(
            "  允许执行? [y]是 / [n]否 / [a]始终允许此命令（直接 Enter=否）\n")

    async def reader() -> None:
        assert input_fn is not None
        while True:
            try:
                line = await input_fn("")
            except KeyboardInterrupt:
                await raw_inbox.put(ReplControl.INTERRUPT)
                continue
            except EOFError:
                await raw_inbox.put(None)
                return
            except asyncio.CancelledError:
                raise
            await raw_inbox.put(line)

    def start_stdin_reader() -> None:
        """真实终端使用 daemon 线程，避免退出时被阻塞的 input() 拖住。"""
        event_loop = asyncio.get_running_loop()

        def push(line) -> None:
            try:
                event_loop.call_soon_threadsafe(raw_inbox.put_nowait, line)
            except RuntimeError:
                pass  # 事件循环已经结束，daemon 线程可自行退出

        def read_stdin() -> None:
            while True:
                input_ready.wait()
                try:
                    state.raw_input = editor.available
                    line = editor.readline(current_prompt)
                except KeyboardInterrupt:
                    line = ReplControl.INTERRUPT
                except (EOFError, OSError):
                    line = None
                input_ready.clear()
                push(line)
                if line is None:
                    return

        threading.Thread(
            target=read_stdin, name="krelcode-stdin", daemon=True,
        ).start()

    async def process_inputs() -> None:
        """后台把原始输入转换为 InputAction，前台只消费标准动作。"""
        while True:
            raw = await raw_inbox.get()
            if raw is None:
                await action_queue.put(None)
                return
            if isinstance(raw, ReplControl):
                await action_queue.put(raw)
                continue
            queue_next = False
            if isinstance(raw, TerminalSubmission):
                queue_next = raw.queue_next
                raw = raw.text
            try:
                action = await processor.process(InputEnvelope(raw))
            except Exception as exc:
                print(f"  （输入处理失败: {type(exc).__name__}: {exc}）")
                action = InputAction(
                    kind=InputKind.EMPTY, raw_text=str(raw), source="terminal")
            if queue_next:
                action = replace(action, delivery=InputDelivery.QUEUE)
            await action_queue.put(action)

    def show_attachment_notes(action: InputAction) -> None:
        for note_text in action.notes:
            print(f"  [@] {note_text}")

    def to_session_prompt(
        action: InputAction,
        *,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
    ) -> SessionPrompt:
        return SessionPrompt.create(
            action.text,
            images=action.images,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids or (),
            label=_queue_label(action),
        )

    quick_task: Optional[asyncio.Task] = None
    quick_unsubscribe: Optional[Callable[[], None]] = None

    def on_session_event(event) -> None:
        renderer(event)
        if isinstance(event, MemoryWritten):
            refresh_memory_cache()
            refresh_status(state)

    runner = ReplSessionRunner(
        controller,
        state,
        on_event=on_session_event,
        on_status=refresh_status,
        on_approval=show_approval,
        on_notice=notice,
        on_stream_end=renderer.end_stream,
        approval_broker=approval_broker,
        input_broker=input_broker,
    )

    async def cancel_quick_task(
        message: str = "⎿ Shell 快捷命令已中止",
        *,
        show_notice: bool = True,
    ) -> None:
        nonlocal quick_task, quick_unsubscribe, quick_status
        if quick_task is None:
            return
        runner.reject_pending_approval()
        if not quick_task.done():
            quick_task.cancel()
        await asyncio.gather(quick_task, return_exceptions=True)
        quick_task = None
        quick_status = "shell · 已中止"
        if quick_unsubscribe is not None:
            quick_unsubscribe()
            quick_unsubscribe = None
        renderer.end_stream()
        if show_notice:
            notice(message)

    async def status_loop() -> None:
        """没有新事件时也持续刷新耗时，避免状态栏停住。"""
        while True:
            await asyncio.sleep(0.5)
            if state.is_running:
                refresh_status(state, force=True)

    async def handle_command(action: InputAction):
        if action.command == "/plan":
            prompt.hide()
            result = handle_plan_command(
                action.arguments,
                controller.plan_manager,
                state,
                allow_actions=not state.is_running,
            )
            for line in result.output:
                print(line)
            return result if result.run_prompt is not None else False
        result = await _handle_command(
            action.text, loop, renderer,
            session_path=session_path, sessions_dir=sessions_dir,
            mcp_supervisor=mcp_supervisor, mcp_reload=mcp_reload,
            subagents=subagents, checkpoints=checkpoints, usage=usage,
            restore_session=None, workspace_root=workspace_root,
            model_switch=model_switch, skills=skills,
            skills_reload=skills_reload, controller=controller,
            command_registry=processor.command_registry,
        )
        if action.command == "/memory":
            refresh_memory_cache()
        return result

    restore_prompt()
    reader_task = asyncio.create_task(reader()) if input_fn is not None else None
    if reader_task is None:
        start_stdin_reader()
    processor_task = asyncio.create_task(process_inputs())
    action_get = asyncio.create_task(action_queue.get())
    job_notice_get = asyncio.create_task(job_notifications.get())
    status_task = asyncio.create_task(status_loop())
    runner.arm_approval_watcher()
    runner.arm_input_watcher()
    try:
        while True:
            wait_set = {action_get, job_notice_get, *runner.wait_tasks()}
            if quick_task is not None:
                wait_set.add(quick_task)
            done, _ = await asyncio.wait(
                wait_set, return_when=asyncio.FIRST_COMPLETED)
            handled = await runner.process_completed(done)

            if quick_task is not None and quick_task in done:
                try:
                    direct_result = quick_task.result()
                    lines = [
                        " ".join(line.split())
                        for line in direct_result.content.splitlines()
                        if line.strip()
                    ]
                    preview = next(
                        (line for line in lines if not line.startswith("exit_code:")),
                        lines[0] if lines else "无输出",
                    )
                    quick_status = (
                        f"shell · {'失败' if direct_result.is_error else '完成'}"
                        f" · {preview[:100]}")
                except asyncio.CancelledError:
                    quick_status = "shell · 已中止"
                except Exception as exc:
                    quick_status = f"shell · {type(exc).__name__}: {exc}"
                quick_task = None
                if quick_unsubscribe is not None:
                    quick_unsubscribe()
                    quick_unsubscribe = None
                renderer.end_stream()
                handled = True

            if job_notice_get in done:
                job_event = job_notice_get.result()
                job_notice_get = asyncio.create_task(job_notifications.get())
                notice(_format_job_notification(job_event))
                handled = True

            if action_get not in done:
                if (handled or runner.pending is not None
                        or runner.has_pending_input):
                    restore_prompt()
                continue
            raw_action = action_get.result()
            action_get = asyncio.create_task(action_queue.get())

            # Shift-Tab：在任意前台状态快速轮换权限模式，保留当前草稿。
            if raw_action is ReplControl.CYCLE_PERMISSION:
                plan_status_before = controller.plan_manager.status.value
                previous, current = controller.cycle_permission_mode()
                suffix = ""
                if (
                    plan_status_before != "cancelled"
                    and controller.plan_manager.status.value == "cancelled"
                ):
                    suffix = "；待确认计划已取消"
                if current in {"plan", "read-only"} and runner.pending is not None:
                    runner.reject_pending_approval()
                    suffix += "；待审批请求已拒绝"
                notice(
                    f"权限模式: {previous} → {current}{suffix}")
                restore_prompt()
                continue

            # Esc/Ctrl-C：运行中中止；空闲时只清空当前输入。
            if isinstance(raw_action, ReplControl):
                prompt.hide()
                if quick_task is not None:
                    await cancel_quick_task()
                    restore_prompt()
                elif runner.is_running:
                    await runner.finish_current(interrupt=True)
                    restore_prompt()
                else:
                    restore_prompt()
                continue

            action = raw_action

            # EOF：停掉运行中任务并退出；不启动已排队消息。
            if action is None:
                prompt.hide()
                if runner.is_running:
                    await runner.finish_current(
                        interrupt=True,
                        run_queued=False,
                        show_stopped_note=False,
                    )
                return 0

            prompt.submitted(raw=state.raw_input)

            # 队列属于当前终端前台状态，必须在审批和会话命令之前截获。
            if action.kind is InputKind.UI_COMMAND and action.command == "/queue":
                prompt.hide()
                manage_prompt_queue(action.arguments, runner.queue, write=print)
                restore_prompt()
                continue

            # ask_user 请求优先消费普通文本；控制命令和展示命令仍可使用。
            if runner.has_pending_input:
                if action.kind is InputKind.UI_COMMAND:
                    prompt.hide()
                    await handle_command(action)
                    restore_prompt()
                    continue
                if action.kind is InputKind.CONTROL:
                    runner.reject_pending_input()
                    if action.command == "/stop" and runner.is_running:
                        await runner.finish_current(interrupt=True)
                    elif action.command == "/exit":
                        if runner.is_running:
                            await runner.finish_current(
                                interrupt=True,
                                run_queued=False,
                                show_stopped_note=False,
                            )
                        return 0
                    else:
                        print("  （当前正在等待用户输入；可输入回答、/stop 或 /exit）")
                        restore_prompt()
                    continue
                prompt.hide()
                runner.resolve_user_input(action.raw_text)
                restore_prompt()
                continue

            # 审批优先消费 y/n/a；/thinking、/tool-output 仍可在审批期间使用。
            if runner.pending is not None:
                if action.kind is InputKind.UI_COMMAND:
                    prompt.hide()
                    await handle_command(action)
                    restore_prompt()
                    continue
                if action.kind is InputKind.CONTROL:
                    runner.reject_pending_approval()
                    if action.command == "/stop" and quick_task is not None:
                        await cancel_quick_task()
                        restore_prompt()
                        continue
                    if action.command == "/stop" and runner.is_running:
                        await runner.finish_current(interrupt=True)
                    elif action.command == "/exit":
                        await cancel_quick_task(show_notice=False)
                        if runner.is_running:
                            await runner.finish_current(
                                interrupt=True,
                                run_queued=False,
                                show_stopped_note=False,
                            )
                        return 0
                    else:
                        print("  （审批输入必须是 y / n / a）")
                        restore_prompt()
                    continue
                prompt.hide()
                runner.resolve_approval(action.raw_text)
                restore_prompt()
                continue

            input_history.add(action.raw_text)

            if action.kind is InputKind.EMPTY:
                restore_prompt()
                continue

            if action.kind is InputKind.MEMORY:
                if not action.text:
                    prompt.hide()
                    for line in memory_lines(memory_cache.entries):
                        print(f"  {line}")
                    panel_controller.focus("memory")
                    restore_prompt()
                    continue
                if runner.is_running or quick_task is not None:
                    notice("⎿ Memory 写入需等待当前任务结束")
                    restore_prompt()
                    continue
                try:
                    scope, note_text = parse_memory_note(action.text)
                    entry = memory_store.add(note_text, scope=scope)
                except (OSError, ValueError) as exc:
                    notice(f"⎿ 无法保存记忆: {exc}")
                    restore_prompt()
                    continue
                sync_memory_fragment(loop, memory_store)
                refresh_memory_cache()
                panel_controller.focus("memory")
                notice(
                    f"⎿ 已保存{SCOPE_LABELS[scope]}记忆 #{entry.id}")
                restore_prompt()
                continue

            if action.kind is InputKind.SHELL:
                if not action.text:
                    notice("用法：!<shell 命令>，例如 !pytest -q")
                    restore_prompt()
                    continue
                if runner.is_running or quick_task is not None:
                    notice("⎿ Shell 快捷命令需等待当前任务结束")
                    restore_prompt()
                    continue
                prompt.hide()
                quick_status = f"shell · 运行中 · {action.text[:100]}"
                quick_unsubscribe = loop.bus.subscribe(renderer)
                quick_task = asyncio.create_task(
                    controller.execute_shell_shortcut(action.text))
                restore_prompt()
                continue

            if quick_task is not None and action.kind is InputKind.CONTROL:
                prompt.hide()
                await cancel_quick_task(show_notice=action.command != "/exit")
                if action.command == "/exit":
                    return 0
                restore_prompt()
                continue

            # RUNNING：控制命令、展示命令、steering 或下一条排队。
            if runner.is_running:
                prompt.hide()
                if action.kind is InputKind.CONTROL:
                    if action.command == "/stop":
                        await runner.finish_current(interrupt=True)
                        restore_prompt()
                        continue
                    if action.command == "/exit":
                        await runner.finish_current(
                            interrupt=True,
                            run_queued=False,
                            show_stopped_note=False,
                        )
                        return 0
                if action.kind is InputKind.UI_COMMAND:
                    await handle_command(action)
                    restore_prompt()
                    continue
                if action.kind is InputKind.COMMAND:
                    spec = processor.command_registry.resolve(action.command)
                    if spec is not None and spec.available_during_run:
                        prompt.hide()
                        await handle_command(action)
                        restore_prompt()
                        continue
                    print(RUNNING_HINT)
                    restore_prompt()
                    continue
                if action.kind is InputKind.PROMPT:
                    show_attachment_notes(action)
                    if action.delivery is InputDelivery.QUEUE:
                        position = runner.queue_prompt(
                            to_session_prompt(action))
                        print(f"  ⎿ 下一条已排队（队列 #{position}，"
                              f"当前任务完成后自动发送）: "
                              f"{_queue_label(action, 60)}")
                    else:
                        preview = controller.steer(
                            action.text, images=action.images or None)
                        print(f"  ⎿ 插话已排队（模型下一步看到）: {preview}")
                restore_prompt()
                continue

            # IDLE：控制命令、普通消息、业务/展示命令。
            if action.kind is InputKind.CONTROL:
                prompt.hide()
                if action.command == "/exit":
                    return 0
                print("  （当前没有运行中的任务）")
                restore_prompt()
                continue

            if action.kind is InputKind.PROMPT:
                prompt.hide()
                show_attachment_notes(action)
                if action.delivery is InputDelivery.QUEUE:
                    position = runner.queue_prompt(to_session_prompt(action))
                    print(f"  ⎿ 下一条已排队（队列 #{position}，"
                          f"当前任务已结束，立即发送）: "
                          f"{_queue_label(action, 60)}")
                    runner.start_queued()
                else:
                    runner.start(to_session_prompt(action))
                restore_prompt()
                continue

            prompt.hide()
            handled_command = await handle_command(action)
            if (isinstance(handled_command, CommandResult)
                    and handled_command.run_prompt is not None):
                skill_prompt = SessionPrompt.create(
                    handled_command.run_prompt,
                    tool_allowlist=handled_command.tool_allowlist,
                    reminder_ids=handled_command.reminder_ids,
                    label=_queue_label(action),
                )
                runner.start(skill_prompt)
            restore_prompt()
    finally:
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)
        if reader_task is not None:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        processor_task.cancel()
        action_get.cancel()
        job_notice_get.cancel()
        unsubscribe_job_notifications()
        await asyncio.gather(
            processor_task, action_get, job_notice_get, return_exceptions=True)
        if quick_task is not None:
            if not quick_task.done():
                quick_task.cancel()
            await asyncio.gather(quick_task, return_exceptions=True)
            quick_task = None
        if quick_unsubscribe is not None:
            quick_unsubscribe()
            quick_unsubscribe = None
        await runner.close()
        if approval_broker is not None:
            approval_broker.cancel_all()
        if input_broker is not None:
            input_broker.cancel_all()
        state.finish_turn()
        prompt.finish()


async def _handle_command(
    user_input,
    loop,
    renderer,
    *,
    session_path,
    sessions_dir,
    mcp_supervisor,
    mcp_reload,
    subagents,
    checkpoints,
    usage,
    restore_session,
    workspace_root,
    model_switch=None,
    skills=None,
    skills_reload=None,
    plugins=None,
    plugins_reload=None,
    controller: Optional[SessionController] = None,
    command_registry: Optional[CommandRegistry] = None,
):
    """兼容入口：终端展示命令留在 UI，业务命令委托 SessionController。"""
    if not user_input.startswith("/"):
        return None

    # 这两类命令操作的是当前终端 renderer 的展示缓存，不属于会话业务状态。
    if user_input == "/thinking":
        if not renderer.expand_thinking():
            print("  （暂无思考内容）")
        return False
    if user_input == "/tool-output" or user_input.startswith("/tool-output "):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            if not renderer.list_tool_outputs():
                print("  （暂无可展开的工具输出）")
        elif not renderer.expand_tool_output(parts[1].strip()):
            print("  （未找到工具输出；输入 /tool-output 查看编号）")
        return False

    if controller is None:
        controller = SessionController(
            loop,
            session_path=session_path,
            sessions_dir=sessions_dir,
            mcp_supervisor=mcp_supervisor,
            mcp_reload=mcp_reload,
            subagents=subagents,
            checkpoints=checkpoints,
            usage=usage,
            model_switch=model_switch,
            skills=skills,
            skills_reload=skills_reload,
            plugins=plugins,
            plugins_reload=plugins_reload,
            workspace_root=workspace_root,
            command_registry=command_registry,
        )
    result = await controller.execute_command(user_input)
    for line in result.output:
        print(line)
    if result.clear_tool_outputs:
        renderer.clear_tool_outputs()
    if result.run_prompt is not None:
        return result
    return False


def _resolve_resume_target(target: str, sessions_dir: Optional[Path]) -> Optional[Path]:
    """兼容旧测试/嵌入方的导入路径。"""
    return resolve_resume_target(target, sessions_dir)
