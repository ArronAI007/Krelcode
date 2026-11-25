"""Prompt rendering and display-width primitives.

This module owns only ANSI-safe drawing primitives.  The raw interactive line
editor composes them in :mod:`repl_primitives`.
"""

from __future__ import annotations

import re
import shutil
import sys
import threading
import unicodedata
from typing import Callable, Optional, TypeAlias

from .render import DIM, RESET, auto_color

IDLE_PROMPT = "❯ "
RUNNING_PROMPT = "↳ "
MENU_ROWS = 8

PromptRenderState: TypeAlias = tuple[str, tuple[str, ...], int, int]


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
        self._last: Optional[PromptRenderState] = None
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
                if (self._last is not None and self._last[0] == text
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
            signature = (text, tuple(suggestions or ()), row, column,
                         self._status)
            if self._last is not None and signature == (
                    *self._last, self._last_status):
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
            if (not redraw or not self.enabled or not self._visible
                    or self._last is None):
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
            had_status = bool(self._status)
            self._status = status
            if not self.enabled or not self._visible or self._last is None:
                return
            if had_status and status:
                self._update_status_in_place()
                self._last_status = status
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
            if self._last is not None:
                text, suggestions, row, column = self._last
                self._render(text, list(suggestions), row, column)
            else:
                self.show(fallback)

    def retarget_prompt(self, old: str, new: str) -> None:
        """切换运行/空闲提示符，同时保留用户已经输入的草稿。"""
        with self._lock:
            if not self.enabled or self._last is None:
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
            status_rows.append(self._status_line())
        menu_capacity = max(0, MENU_ROWS - len(status_rows))
        menu = list(suggestions[:menu_capacity])
        menu.extend([""] * (menu_capacity - len(menu)))
        self._input_rows = len(input_lines)
        return [*self._panel_lines, rule, *input_lines, rule, *status_rows, *menu]

    def _status_line(self) -> str:
        columns = shutil.get_terminal_size((80, 24)).columns
        status = _truncate_display(
            f"  {self._status}", max(1, columns - 1))
        return f"{DIM}{status}{RESET}" if self._color else status

    def _update_status_in_place(self) -> None:
        """Refresh only the status row, leaving panel and input borders intact."""
        status_row = len(self._panel_lines) + self._input_rows + 2
        distance = status_row - self._cursor_row
        self._write("\r")
        if distance > 0:
            self._write(self.CURSOR_DOWN * distance)
        elif distance < 0:
            self._write(self.CURSOR_UP * -distance)
        self._write(self.CLEAR_LINE)
        self._write(self._status_line())
        self._write("\r")
        if distance > 0:
            self._write(self.CURSOR_UP * distance)
        elif distance < 0:
            self._write(self.CURSOR_DOWN * -distance)

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
        """Keep the submitted input, clear the menu, and return to its end."""
        with self._lock:
            was_visible = self.enabled and self._visible
            if was_visible:
                self._move_to_top()
                self._write("\r")
                after_content = (
                    len(self._panel_lines)
                    + self._input_rows
                    + 2
                    + (1 if self._status else 0)
                )
                self._write(self.CURSOR_DOWN * after_content)
                self._write(self.CLEAR_BELOW)
            self._visible = False
            self._last = None
            self._last_status = ""
            self._submitted = was_visible

    def hide(self) -> None:
        with self._lock:
            if not self.enabled or not self._visible:
                return
            self._move_to_top()
            self._write(self.CLEAR_BELOW)
            self._visible = False
            self._last = None
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


__all__ = [
    "IDLE_PROMPT",
    "MENU_ROWS",
    "PromptRenderState",
    "PromptWriter",
    "RUNNING_PROMPT",
    "_display_width",
    "_input_rule",
    "_pad_display",
    "_stdout_write",
    "_truncate_display",
]
