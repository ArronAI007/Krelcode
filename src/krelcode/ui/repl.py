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
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

try:  # POSIX TTY；Windows 自动退回普通 input()
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None
    tty = None

from .. import __version__
from ..agent.loop import AgentLoop
from ..agent.query import QuerySession
from ..agent.runtime import AgentRuntime
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
from ..session.manager import resolve_resume_target
from ..session.state import SessionViewState
from .approval import ApprovalBroker, ApprovalRequest, ALWAYS, YES
from .editor import (
    EditBuffer,
    HistorySearch,
    InputHistory,
    sanitize_pasted_text,
)
from .queue import PromptQueue
from .render import CYAN, DIM, RESET, StreamRenderer, auto_color

RUNNING_HINT = (
    "  （任务运行中：Enter=插话，Alt+Enter=排队下一条，Esc/Ctrl-C 中止；"
    "/thinking 展开思考，/tool-output <编号> 展开输出，/queue 管理队列，"
    "/stop 中止，/exit 退出）"
)
STOPPED_NOTE = "  ⎿ 已中止（历史已修复，可继续对话）"

IDLE_PROMPT = "❯ "
RUNNING_PROMPT = "↳ "
MENU_ROWS = 8


class ReplControl(str, Enum):
    """终端控制键产生的非文本动作。"""

    INTERRUPT = "interrupt"


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
                         limit: int = 8,
                         registry: Optional[CommandRegistry] = None
                         ) -> list[tuple[str, str]]:
    """返回与当前输入前缀匹配的 slash 命令；进入参数后不再弹菜单。"""
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    catalog = registry or _BUILTIN_COMMANDS
    return [(spec.invocation, spec.description)
            for spec in catalog.list(query=text, running=running)[:limit]]


QUEUE_USAGE = "用法：/queue | /queue delete <n> | /queue move <n> <m> | /queue clear"


def _queue_label(action: InputAction, limit: int = 100) -> str:
    text = " ".join(action.raw_text.split()) or action.text
    return _truncate_display(text, limit)


def _format_prompt_queue(queue: PromptQueue[InputAction]) -> list[str]:
    if not queue:
        return ["下一条消息队列为空"]
    lines = [f"下一条消息队列（{len(queue)}）:"]
    for index, action in enumerate(queue.entries, 1):
        lines.append(f"  #{index} {_queue_label(action)}")
    return lines


def manage_prompt_queue(
    arguments: str,
    queue: PromptQueue[InputAction],
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
                 color: Optional[bool] = None) -> None:
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

    @property
    def _rule(self) -> str:
        """当前终端宽度下的输入区边界，供渲染诊断与测试使用。"""
        return _input_rule(self._color)

    def show(self, text: str) -> None:
        if not self.enabled:
            return
        if self._visible:
            if self._last and self._last[0] == text and not self._last[1]:
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
        if not self.enabled or not self._visible:
            return
        lines = text.split("\n")
        row = len(lines) - 1 if cursor_row is None else max(0, min(cursor_row, len(lines) - 1))
        column = (_display_width(lines[row]) if cursor_column is None
                  else max(0, cursor_column))
        signature = (text, tuple(suggestions or ()), row, column)
        if signature == self._last:
            return
        self._render(text, suggestions or [], row, column)

    @staticmethod
    def _end_position(text: str) -> tuple[int, int]:
        lines = text.split("\n")
        return len(lines) - 1, _display_width(lines[-1])

    def _block(self, text: str, suggestions: list[str]) -> list[str]:
        rule = _input_rule(self._color)
        input_lines = text.split("\n")
        menu = list(suggestions[:MENU_ROWS])
        menu.extend([""] * (MENU_ROWS - len(menu)))
        self._input_rows = len(input_lines)
        return [rule, *input_lines, rule, *menu]

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
        target_row = 1 + cursor_row
        moves_up = self._rendered_rows - 1 - target_row
        self._write("\r")
        if moves_up > 0:
            self._write(self.CURSOR_UP * moves_up)
        if cursor_column > 0:
            self._write(f"\x1b[{cursor_column}C")
        self._visible = True
        self._cursor_row = target_row
        self._prompt_width = cursor_column
        self._suggestions = tuple(suggestions[:MENU_ROWS])
        self._last = (text, tuple(suggestions), cursor_row, cursor_column)

    def submitted(self, *, raw: bool = True) -> None:
        """保留输入框，清掉菜单区，把输出光标放到下边界之后。"""
        was_visible = self.enabled and self._visible
        if was_visible:
            self._move_to_top()
            output_row = 2 + self._input_rows
            self._write("\r")
            if output_row > 0:
                self._write(self.CURSOR_DOWN * output_row)
            self._write(self.CLEAR_BELOW)
        self._visible = False
        self._last = ""
        self._submitted = was_visible

    def hide(self) -> None:
        if not self.enabled or not self._visible:
            return
        self._move_to_top()
        self._write(self.CLEAR_BELOW)
        self._visible = False
        self._last = ""
        self._cursor_row = 0

    def finish(self) -> None:
        """退出前结束当前终端行，不在 shell 留下孤立空行。"""
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
        b"[3~", b"[3;5~", b"[200~", b"[13;2u", b"[13;3u",
        b"[27;2;13~", b"[27;3;13~",
        b"b", b"B", b"f", b"F", b"\x7f", b"\r", b"\n",
    })

    def __init__(self, prompt: PromptWriter,
                 suggest: Callable[[str], list[tuple[str, str]]],
                 color: bool = False,
                 history: Optional[InputHistory] = None) -> None:
        self._prompt = prompt
        self._suggest = suggest
        self._color = color
        self._history = history if history is not None else InputHistory()
        self._pending_input = bytearray()

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
        termios.tcsetattr(fd, termios.TCSADRAIN, editor_settings)
        self._set_bracketed_paste(fd, True)
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
                elif char == "\t":
                    self._complete(buffer)
                elif char >= " " and char != "\x7f":
                    buffer.insert(char)
                self._refresh(prompt_text(), buffer, search=search)
        finally:
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
        while len(sequence) < 16:
            if not self._pending_input and not select.select(
                [fd], [], [], 0.01
            )[0]:
                break
            sequence.extend(self._read_one(fd))
            if bytes(sequence) in TerminalLineEditor.COMPLETE_ESCAPE_SEQUENCES:
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
    write(f"  {accent('/help')} 查看命令 · ↑/↓ 历史 · Ctrl-R 搜索 · "
          f"{accent('@路径')} 引用文件 · {accent('/exit')} 退出")
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
    input_fn=None,
    redactor=None,
    runtime: Optional[AgentRuntime] = None,
    input_processor: Optional[InputProcessor] = None,
    approval_broker: Optional[ApprovalBroker] = None,
    history_path: Optional[Path] = None,
    persist_history: Optional[bool] = None,
) -> int:
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
        # 任何异步输出前先清掉提示符，输出后再由主循环决定是否重绘
        prompt.hide()
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
        workspace_root=workspace_root,
        command_registry=processor.command_registry,
    )

    print_banner(loop, tool_names, session_path=session_path)

    query: Optional[QuerySession] = None
    render_task: Optional[asyncio.Task] = None
    approval_task: Optional[asyncio.Task] = None
    pending_approval: Optional[ApprovalRequest] = None
    queued_prompts: PromptQueue[InputAction] = PromptQueue()
    state = SessionViewState()
    raw_inbox: asyncio.Queue = asyncio.Queue()
    action_queue: asyncio.Queue = asyncio.Queue()
    input_ready = threading.Event()

    def current_prompt() -> str:
        text = RUNNING_PROMPT if state.is_running else IDLE_PROMPT
        return f"{CYAN}{text}{RESET}" if use_color else text

    def suggest(text: str) -> list[tuple[str, str]]:
        return match_slash_commands(
            text, running=state.is_running, registry=processor.command_registry)

    editor = TerminalLineEditor(
        prompt, suggest, color=use_color, history=input_history)

    def restore_prompt() -> None:
        prompt.show(current_prompt())
        input_ready.set()

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

    async def consume_query_events(session: QuerySession) -> None:
        async for item in session.events:
            renderer(item.event)

    def launch_prompt(
        action: InputAction,
        *,
        tool_allowlist: Optional[list[str]] = None,
    ) -> tuple[QuerySession, asyncio.Task]:
        show_attachment_notes(action)
        session = controller.start_query(
            action.text,
            images=list(action.images) or None,
            tool_allowlist=tool_allowlist,
        )
        state.begin_turn(turn_id=session.turn_id)
        if prompt.enabled:
            status = "  ⋯ 工作中 · Enter=插话 · Alt+Enter=下一条 · Esc/Ctrl-C 中止"
            print(f"{DIM}{status}{RESET}" if use_color else status)
        consumer = asyncio.create_task(consume_query_events(session))
        return session, consumer

    def watch_approval() -> Optional[asyncio.Task]:
        """只在没有 pending 请求时等待 broker 的下一个审批项。"""
        if approval_broker is None or pending_approval is not None:
            return None
        if approval_task is not None:
            return approval_task
        return asyncio.create_task(approval_broker.next_request())

    def redact_value(value) -> str:
        text = str(value)
        return redactor.redact(text) if redactor is not None else text

    def show_approval(request: ApprovalRequest) -> None:
        tool_call = request.tool_call
        command = tool_call.arguments.get("command")
        detail = command if command is not None else tool_call.arguments
        print(f"  [审批] {tool_call.name}: {redact_value(detail)}")
        print(f"  原因: {redact_value(request.reason)}")
        print("  允许执行? [y]是 / [n]否 / [a]始终允许此命令（直接 Enter=否）")

    def resolve_approval(raw: str) -> bool:
        nonlocal pending_approval, approval_task
        if approval_broker is None or pending_approval is None:
            return False
        answer = approval_broker.resolve(raw)
        if answer is None:
            # 发起审批的后台任务可能已被取消，Future 已失效。
            pending_approval = None
            print("  ⎿ 审批请求已取消或过期")
            approval_task = watch_approval()
            return True
        pending_approval = None
        allowed = answer in (YES, ALWAYS)
        print(f"  ⎿ 审批结果: {'已允许' if allowed else '已拒绝'}")
        approval_task = watch_approval()
        return True

    def reject_pending_approval() -> None:
        nonlocal pending_approval, approval_task
        if approval_broker is not None:
            approval_broker.cancel_all()
        pending_approval = None
        if approval_task is not None and not approval_task.done():
            approval_task.cancel()
        approval_task = None

    def drain_steering_notes() -> None:
        """任务结束后丢弃未送达的插话（可见地），避免悄悄留到下一轮。"""
        for leftover in controller.drain_steering():
            text = getattr(leftover, "text", leftover)
            print(f"  ⎿ 任务已结束，未送达的插话已丢弃: {str(text)[:60]}")

    def start_queued_prompt() -> None:
        """当前 turn 收尾后自动启动 Alt+Enter 排队的下一条消息。"""
        nonlocal query, render_task, approval_task
        if query is not None or not queued_prompts:
            return
        action = queued_prompts.popleft()
        if queued_prompts:
            print(f"  ⎿ 启动下一条消息；队列还剩 {len(queued_prompts)} 条")
        query, render_task = launch_prompt(action)
        approval_task = watch_approval()

    async def finish_current(
        *,
        interrupt: bool = False,
        run_queued: bool = True,
        show_stopped_note: bool = True,
    ) -> None:
        """统一收尾当前 query：可选中止、等待事件 drain，再启动下一条。"""
        nonlocal query, render_task, approval_task, pending_approval
        if query is None:
            return
        prompt.hide()
        current = query
        consumer = render_task
        watcher = approval_task
        if interrupt and not current.task.done():
            state.request_stop()
            reject_pending_approval()
            await controller.stop()
        elif interrupt:
            reject_pending_approval()
        if interrupt:
            if watcher is not None and not watcher.done():
                watcher.cancel()
            if watcher is not None:
                await asyncio.gather(watcher, return_exceptions=True)
        if consumer is not None:
            await asyncio.gather(consumer, return_exceptions=True)

        cancelled = current.task.cancelled()
        error: Optional[BaseException] = None
        if not cancelled:
            try:
                current.task.result()
            except BaseException as exc:  # noqa: BLE001 - UI 必须展示所有终态
                error = exc
        if cancelled or error is not None:
            renderer.end_stream()
        if cancelled and show_stopped_note:
            print(STOPPED_NOTE)
        elif error is not None:
            print(f"错误：{type(error).__name__}: {error}")

        state.finish_turn()
        query = None
        render_task = None
        if interrupt:
            approval_task = None
        pending_approval = None
        drain_steering_notes()
        if run_queued:
            start_queued_prompt()
        if approval_task is None:
            approval_task = watch_approval()
        restore_prompt()

    async def handle_command(action: InputAction):
        return await _handle_command(
            action.text, loop, renderer,
            session_path=session_path, sessions_dir=sessions_dir,
            mcp_supervisor=mcp_supervisor, mcp_reload=mcp_reload,
            subagents=subagents, checkpoints=checkpoints, usage=usage,
            restore_session=None, workspace_root=workspace_root,
            model_switch=model_switch, skills=skills,
            skills_reload=skills_reload, controller=controller,
            command_registry=processor.command_registry,
        )

    restore_prompt()
    reader_task = asyncio.create_task(reader()) if input_fn is not None else None
    if reader_task is None:
        start_stdin_reader()
    processor_task = asyncio.create_task(process_inputs())
    action_get = asyncio.create_task(action_queue.get())
    approval_task = watch_approval()
    try:
        while True:
            if (pending_approval is not None and approval_broker is not None
                    and approval_broker.pending is None):
                pending_approval = None
                print("  ⎿ 审批请求已取消或过期")
                approval_task = watch_approval()
            wait_set = {action_get}
            if approval_task is not None:
                wait_set.add(approval_task)
            if query is not None:
                wait_set.add(query.task)
                if render_task is not None:
                    wait_set.add(render_task)
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            # 1) 安全策略请求审批：由当前前台输入循环接管，避免与 stdin reader 竞争。
            if approval_task is not None and approval_task in done:
                try:
                    request = approval_task.result()
                except asyncio.CancelledError:
                    request = None
                approval_task = None
                if request is not None:
                    pending_approval = request
                    prompt.hide()
                    show_approval(request)
                    restore_prompt()

            # 2) 查询结束或事件消费者异常退出。先 drain 全部 QueryEvent，
            # 再允许进入下一条输入。
            consumer_done = render_task is not None and render_task in done
            if query is not None and (query.task in done or consumer_done):
                await finish_current()

            if action_get not in done:
                continue
            raw_action = action_get.result()
            action_get = asyncio.create_task(action_queue.get())

            # 3) Esc/Ctrl-C：运行中中止；空闲时只清空当前输入。reader 会产生
            # 非文本控制动作，因此这里不能把它当作用户消息处理。
            if isinstance(raw_action, ReplControl):
                prompt.hide()
                if query is not None:
                    await finish_current(interrupt=True)
                else:
                    restore_prompt()
                continue

            action = raw_action

            # 4) EOF：停掉运行中任务并退出；不启动已排队消息。
            if action is None:
                prompt.hide()
                if query is not None:
                    await finish_current(
                        interrupt=True,
                        run_queued=False,
                        show_stopped_note=False,
                    )
                return 0

            prompt.submitted(raw=state.raw_input)

            # 队列属于当前终端前台状态，必须在审批和会话命令之前截获。
            if action.kind is InputKind.UI_COMMAND and action.command == "/queue":
                prompt.hide()
                manage_prompt_queue(action.arguments, queued_prompts, write=print)
                restore_prompt()
                continue

            # 5) 审批优先消费 y/n/a；/thinking、/tool-output 仍可在审批期间使用。
            if pending_approval is not None:
                if action.kind is InputKind.UI_COMMAND:
                    prompt.hide()
                    await handle_command(action)
                    restore_prompt()
                    continue
                if action.kind is InputKind.CONTROL:
                    reject_pending_approval()
                    if action.command == "/stop" and query is not None:
                        await finish_current(interrupt=True)
                    elif action.command == "/exit":
                        if query is not None:
                            await finish_current(
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
                resolve_approval(action.raw_text)
                restore_prompt()
                continue

            input_history.add(action.raw_text)

            if action.kind is InputKind.EMPTY:
                restore_prompt()
                continue

            # 6) RUNNING：控制命令、展示命令、steering 或下一条排队。
            if query is not None:
                prompt.hide()
                if action.kind is InputKind.CONTROL:
                    if action.command == "/stop":
                        await finish_current(interrupt=True)
                        continue
                    if action.command == "/exit":
                        await finish_current(
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
                    print(RUNNING_HINT)
                    restore_prompt()
                    continue
                if action.kind is InputKind.PROMPT:
                    show_attachment_notes(action)
                    if action.delivery is InputDelivery.QUEUE:
                        position = queued_prompts.append(action)
                        print(f"  ⎿ 下一条已排队（队列 #{position}，"
                              f"当前任务完成后自动发送）: "
                              f"{_queue_label(action, 60)}")
                    else:
                        preview = controller.steer(
                            action.text, images=action.images or None)
                        print(f"  ⎿ 插话已排队（模型下一步看到）: {preview}")
                restore_prompt()
                continue

            # 7) IDLE：控制命令、普通消息、业务/展示命令。
            if action.kind is InputKind.CONTROL:
                prompt.hide()
                if action.command == "/exit":
                    return 0
                print("  （当前没有运行中的任务）")
                restore_prompt()
                continue

            if action.kind is InputKind.PROMPT:
                prompt.hide()
                query, render_task = launch_prompt(action)
                approval_task = watch_approval()
                restore_prompt()
                continue

            prompt.hide()
            handled = await handle_command(action)
            if isinstance(handled, CommandResult) and handled.run_prompt is not None:
                # /skill 已加载完整指令，不再把 Skill 正文中的 @ 当文件引用。
                skill_prompt = InputAction(
                    kind=InputKind.PROMPT,
                    text=handled.run_prompt,
                    raw_text=action.raw_text,
                    source=action.source,
                )
                query, render_task = launch_prompt(
                    skill_prompt,
                    tool_allowlist=(
                        list(handled.tool_allowlist)
                        if handled.tool_allowlist is not None else None
                    ),
                )
                approval_task = watch_approval()
            restore_prompt()
    finally:
        if reader_task is not None:
            reader_task.cancel()
        processor_task.cancel()
        action_get.cancel()
        if approval_task is not None:
            approval_task.cancel()
            await asyncio.gather(approval_task, return_exceptions=True)
        if approval_broker is not None:
            approval_broker.cancel_all()
        if query is not None:
            await query.cancel()
        if render_task is not None:
            await asyncio.gather(render_task, return_exceptions=True)
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
