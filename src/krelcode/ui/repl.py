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
import shlex
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

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
from .diff_review import (
    DiffPager,
    DiffReview,
    build_tool_call_review,
    collect_git_review,
)
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
    APPROVE_DIFF = "approve-diff"
    REJECT_DIFF = "reject-diff"


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
        b"[3~", b"[3;5~", b"[5~", b"[6~", b"[200~", b"[Z", b"[13;2u", b"[13;3u",
        b"[27;2;13~", b"[27;3;13~",
        b"b", b"B", b"f", b"F", b"\x7f", b"\r", b"\n",
    })

    def __init__(self, prompt: PromptWriter,
                 suggest: Callable[[str], list[tuple[str, str]]],
                 color: bool = False,
                 history: Optional[InputHistory] = None,
                 panels: Optional[PanelController] = None,
                 on_diff_pager_change: Optional[Callable[[], None]] = None) -> None:
        self._prompt = prompt
        self._suggest = suggest
        self._color = color
        self._history = history if history is not None else InputHistory()
        self._panels = panels
        self._diff_pager: Optional[DiffPager] = None
        self._on_diff_pager_change = on_diff_pager_change
        self._pending_input = bytearray()
        if panels is not None:
            prompt.set_cursor_probe(True)

    def set_diff_pager(self, pager: Optional[DiffPager]) -> None:
        self._diff_pager = pager

    def _handle_diff_key(self, key: str) -> Optional[ReplControl]:
        pager = self._diff_pager
        if pager is None or not pager.active:
            return None
        action = pager.handle_key(key)
        if action == "approve":
            return ReplControl.APPROVE_DIFF
        if action == "reject":
            return ReplControl.REJECT_DIFF
        if self._on_diff_pager_change is not None:
            self._on_diff_pager_change()
        return None

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
                pager = self._diff_pager
                if pager is not None and pager.active:
                    if data == b"\x1b":
                        sequence = self._read_escape_sequence(fd)
                        if parse_sgr_mouse(sequence) is not None:
                            self._refresh(prompt_text(), buffer, search=search)
                            continue
                        key = {
                            b"": "escape",
                            b"[A": "up", b"OA": "up",
                            b"[B": "down", b"OB": "down",
                            b"[C": "]", b"OC": "]",
                            b"[D": "[", b"OD": "[",
                            b"[5~": "pageup", b"[6~": "pagedown",
                            b"[H": "home", b"OH": "home",
                            b"[F": "end", b"OF": "end",
                            b"[Z": "shift-tab", b"\t": "tab",
                        }.get(sequence, "")
                    else:
                        char = decoder.decode(data)
                        if not char:
                            continue
                        if char == "\x03":
                            return ReplControl.INTERRUPT
                        key = {
                            "\r": "enter", "\n": "enter",
                            "\t": "tab", "\x7f": "b",
                        }.get(char, char)
                    control = self._handle_diff_key(key)
                    if control is not None:
                        return control
                    self._refresh(prompt_text(), buffer, search=search)
                    continue
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
    """Compatibility entrypoint for the extracted conversation host."""
    from .conversation_host import ConversationHost

    host = ConversationHost(
        loop,
        tool_names=tool_names,
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
        input_fn=input_fn,
        redactor=redactor,
        runtime=runtime,
        input_processor=input_processor,
        approval_broker=approval_broker,
        input_broker=input_broker,
        history_path=history_path,
        persist_history=persist_history,
    )
    return await host.run()


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
    diff_handler: Optional[Callable[[str], Awaitable[bool]]] = None,
):
    """兼容入口：终端展示命令留在 UI，业务命令委托 SessionController。"""
    if not user_input.startswith("/"):
        return None

    if user_input == "/diff" or user_input.startswith("/diff "):
        arguments = user_input.partition(" ")[2]
        if diff_handler is not None:
            await diff_handler(arguments)
        else:
            print("  （当前前台不支持 diff 审阅器）")
        return False

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
