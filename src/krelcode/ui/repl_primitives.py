"""Low-level terminal input primitives shared by REPL adapters.

This module deliberately has no dependency on ``ui.repl``, the conversation
host, or the terminal presenter.  It owns prompt drawing, the raw terminal line
editor, and display-width helpers so higher-level adapters can compose them
without an import cycle.
"""

from __future__ import annotations

import codecs
import os
import re
import select
import shutil
import sys
import threading
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

termios: Optional[ModuleType]
tty: Optional[ModuleType]
try:  # POSIX TTY；Windows 自动退回普通 input()
    import termios as _termios
    import tty as _tty
except ImportError:  # pragma: no cover - Windows
    termios = None
    tty = None
else:
    termios = _termios
    tty = _tty

from .diff_review import DiffPager
from .editor import (
    EditBuffer,
    HistorySearch,
    InputHistory,
    sanitize_pasted_text,
)
from .render import CYAN, DIM, RESET, auto_color
from .tui.controller import PanelController
from .tui.mouse import (
    MOUSE_DISABLE,
    MOUSE_ENABLE,
    MouseEvent,
    parse_cursor_position,
    parse_sgr_mouse,
)
from .prompt_writer import (
    IDLE_PROMPT,
    MENU_ROWS,
    RUNNING_PROMPT,
    PromptWriter,
    _display_width,
    _input_rule,
    _pad_display,
    _stdout_write,
    _truncate_display,
)

RUNNING_HINT = (
    "  （任务运行中：Enter=插话，Alt+Enter=排队下一条，Esc/Ctrl-C 中止；"
    "/thinking 展开思考，/plan 查看计划，/tool-output <编号> 展开输出，"
    "/jobs 管理后台任务，/queue 管理队列，/stop 中止，/exit 退出）"
)

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
                 on_diff_pager_change: Optional[Callable[[], None]] = None,
                 mouse_reporting: bool = False,
                 on_mouse_click: Optional[Callable[[MouseEvent], bool]] = None) -> None:
        self._prompt = prompt
        self._suggest = suggest
        self._color = color
        self._history = history if history is not None else InputHistory()
        self._panels = panels
        self._mouse_reporting = mouse_reporting
        self._on_mouse_click = on_mouse_click
        self._diff_pager: Optional[DiffPager] = None
        self._on_diff_pager_change = on_diff_pager_change
        self._pending_input = bytearray()

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
        assert termios is not None and tty is not None
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
            # Mouse reporting captures click-drag and prevents the terminal
            # emulator's native text selection/copy.  Clear stale state and
            # only opt in when explicitly requested.
            self._set_mouse_reporting(fd, False)
            if self._mouse_reporting:
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
                        mouse_event = (
                            parse_sgr_mouse(sequence)
                            if self._mouse_reporting else None
                        )
                        if mouse_event is not None:
                            terminal_size = shutil.get_terminal_size((80, 24))
                            self._panels.set_prompt_origin(
                                terminal_size.lines,
                                self._prompt.rendered_rows,
                            )
                            handled = (
                                self._on_mouse_click(mouse_event)
                                if self._on_mouse_click is not None else False
                            )
                            if not handled:
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
        if isinstance(buffer, list):
            state = EditBuffer("".join(buffer))
        else:
            state = buffer

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
        if isinstance(buffer, list):
            buffer[:] = list(state.text)
        return None

    def _navigate_history(
        self, sequence: bytes, buffer: EditBuffer | list[str]
    ) -> bool:
        """应用 ↑/↓；多行文本内优先移动光标行。"""
        if isinstance(buffer, list):
            state = EditBuffer("".join(buffer))
        else:
            state = buffer

        if sequence in (b"[A", b"OA"):
            if "\n" in state.text and state.move_line_up():
                if isinstance(buffer, list):
                    buffer[:] = list(state.text)
                return True
            state.set_text(self._history.previous(state.text))
            if isinstance(buffer, list):
                buffer[:] = list(state.text)
            return True
        if sequence in (b"[B", b"OB"):
            if "\n" in state.text and state.move_line_down():
                if isinstance(buffer, list):
                    buffer[:] = list(state.text)
                return True
            state.set_text(self._history.next(state.text))
            if isinstance(buffer, list):
                buffer[:] = list(state.text)
            return True
        return False

    def _complete(self, buffer: EditBuffer | list[str]) -> None:
        if isinstance(buffer, list):
            state = EditBuffer("".join(buffer))
        else:
            state = buffer
        matches = [name for name, _ in self._suggest(state.text)]
        if not matches:
            return
        if len(matches) == 1:
            state.set_text(matches[0] + " ")
        else:
            common = os.path.commonprefix(matches)
            if len(common) > len(state.text):
                state.set_text(common)
        if isinstance(buffer, list):
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
            self._prompt.set_panels(
                layout.lines if self._panels.dock_visible else (),
                redraw=False,
            )
        self._prompt.update(
            "\n".join(rendered_lines),
            lines,
            cursor_row=cursor_line,
            cursor_column=cursor_column,
        )


__all__ = [
    "CYAN",
    "DIM",
    "IDLE_PROMPT",
    "MENU_ROWS",
    "PanelController",
    "PromptWriter",
    "RESET",
    "RUNNING_HINT",
    "RUNNING_PROMPT",
    "ReplControl",
    "TerminalLineEditor",
    "TerminalSubmission",
    "_display_width",
    "_input_rule",
    "_pad_display",
    "_stdout_write",
    "_truncate_display",
    "auto_color",
]
