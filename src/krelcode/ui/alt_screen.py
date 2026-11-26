"""Application-owned fullscreen terminal backend.

The native terminal mode leaves scrollback, selection, and text copying to the
terminal emulator.  This backend instead owns a bounded viewport, transcript,
selection state, wheel handling, and clipboard writes while keeping the REPL
editor as the input authority.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional, TextIO

from .prompt_writer import PromptWriter, _display_width
from .render import DIM, RESET, auto_color
from .terminal_markdown import render_markdown
from .tui.mouse import MouseEvent

ENTER_ALT_SCREEN = "\x1b[?1049h"
LEAVE_ALT_SCREEN = "\x1b[?1049l"
ENABLE_BUTTON_MOUSE = "\x1b[?1002h\x1b[?1006h"
DISABLE_BUTTON_MOUSE = "\x1b[?1002l\x1b[?1006l"
SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
CLEAR_SCREEN = "\x1b[2J\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_REWRITE_RE = re.compile(r"\r\x1b\[2K")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def visible_width(text: str) -> int:
    return _display_width(text)


def fit_line(text: str, width: int) -> str:
    """Fit an ANSI line to a terminal width without splitting escape codes."""
    if width <= 0:
        return ""
    plain = strip_ansi(text)
    current = 0
    end = 0
    for index, char in enumerate(plain):
        char_width = 2 if _display_width(char) == 2 else 1
        if current + char_width > width:
            break
        current += char_width
        end = index + 1
    if end == len(plain):
        return text + " " * max(0, width - current)
    return _truncate_ansi(text, width)


def _truncate_ansi(text: str, width: int) -> str:
    out: list[str] = []
    used = 0
    index = 0
    while index < len(text):
        if text[index] == "\x1b":
            match = _ANSI_RE.match(text, index)
            if match:
                out.append(match.group(0))
                index = match.end()
                continue
        char = text[index]
        char_width = 2 if _display_width(char) == 2 else 1
        if used + char_width > max(0, width - 1):
            out.append("…")
            break
        out.append(char)
        used += char_width
        index += 1
    out.append(RESET if "\x1b[" in text else "")
    rendered = "".join(out)
    return rendered + " " * max(0, width - visible_width(rendered))


@dataclass
class SelectionPoint:
    row: int
    column: int


@dataclass
class ThinkingBlock:
    row: int
    content: str
    expanded: bool = False


class _TranscriptWriter(io.TextIOBase):
    def __init__(self, surface: "AltScreenSurface") -> None:
        self.surface = surface

    def isatty(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self.surface.write_transcript(text)
        return len(text)

    def flush(self) -> None:
        return None


class AltScreenSurface:
    """Fullscreen viewport with application-owned scrolling and selection."""

    def __init__(
        self,
        *,
        write: Optional[Callable[[str], None]] = None,
        clipboard: Optional[Callable[[str], bool]] = None,
        color: Optional[bool] = None,
    ) -> None:
        self._terminal_stream = sys.stdout
        self._terminal_write = write or self._write_terminal
        self._clipboard = clipboard or self._system_clipboard
        self._color = auto_color() if color is None else color
        self._active = False
        self._old_stdout: Optional[TextIO] = None
        self._proxy = _TranscriptWriter(self)
        self.transcript: list[str] = []
        self._pending = ""
        self.scroll_top = 0
        self.follow_end = True
        self._viewport_height = 1
        self._viewport_width = 1
        self._clickables: dict[int, tuple[str, str]] = {}
        self._thinking_blocks: dict[int, ThinkingBlock] = {}
        self._thinking_turn_text = ""
        self._latest_thinking_row: Optional[int] = None
        self._thinking_segment_active = False
        self._visible_clickables: dict[int, tuple[str, str]] = {}
        self._on_click: Optional[Callable[[str, str], bool]] = None
        self._selection_anchor: Optional[SelectionPoint] = None
        self._selection_focus: Optional[SelectionPoint] = None
        self._selection_dragged = False
        self._last_render = ""

        self.input_text = ""
        self.input_visible = False
        self.input_rows = 1
        self.input_cursor_row = 0
        self.input_cursor_column = 0
        self.status = ""
        self.panels: tuple[str, ...] = ()
        self.suggestions: tuple[str, ...] = ()

    def _write_terminal(self, text: str) -> None:
        self._terminal_stream.write(text)
        self._terminal_stream.flush()

    @staticmethod
    def _system_clipboard(text: str) -> bool:
        commands: list[list[str]]
        if sys.platform == "darwin":
            commands = [["pbcopy"]]
        elif os.name == "nt":
            commands = [["clip"]]
        else:
            commands = [
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ]
        for command in commands:
            if shutil.which(command[0]) is None:
                continue
            try:
                completed = subprocess.run(
                    command,
                    input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if completed.returncode == 0:
                    return True
            except (OSError, subprocess.SubprocessError):
                continue
        return False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._old_stdout = sys.stdout
        sys.stdout = self._proxy
        self._terminal_write(ENTER_ALT_SCREEN + ENABLE_BUTTON_MOUSE + SHOW_CURSOR)
        self.render(force=True)

    def stop(self, *, print_final: bool = True) -> None:
        if not self._active:
            return
        self._flush_pending()
        final = "\n".join(strip_ansi(line) for line in self.transcript).rstrip()
        self._terminal_write(DISABLE_BUTTON_MOUSE + LEAVE_ALT_SCREEN + RESET)
        if self._old_stdout is not None:
            sys.stdout = self._old_stdout
            self._old_stdout = None
        self._active = False
        if print_final and final:
            self._terminal_write(final + "\n")

    def set_click_handler(
        self,
        callback: Callable[[str, str], bool],
    ) -> None:
        self._on_click = callback

    def _content_lines(self) -> list[str]:
        lines, _clicks = self._render_content()
        return lines

    def _render_content(
        self,
    ) -> tuple[list[str], dict[int, tuple[str, str]]]:
        source = (
            [*self.transcript, self._pending]
            if self._pending else list(self.transcript)
        )
        lines: list[str] = []
        clicks: dict[int, tuple[str, str]] = {}
        for row, line in enumerate(source):
            block = self._thinking_blocks.get(row)
            if block is not None:
                marker = "▼" if block.expanded else "▶"
                action = "点击折叠" if block.expanded else "点击展开"
                label = f"{marker} ✻ Thinking · {action}"
                if self._color:
                    label = f"{DIM}{label}{RESET}"
                lines.append(label)
                clicks[len(lines) - 1] = ("thinking", str(row))
                if block.expanded:
                    rendered = render_markdown(block.content, self._color)
                    lines.extend(f"  {item}" for item in rendered.splitlines())
                continue
            lines.append(line)
            target = self._clickables.get(row)
            if target is not None:
                clicks[len(lines) - 1] = target
        return lines, clicks

    def mark_tool(self, index: int) -> None:
        self._mark_current("tool", str(index))

    def mark_thinking(self) -> None:
        if self._pending:
            self._flush_pending()
        row = len(self.transcript) - 1
        if row < 0:
            return
        self._thinking_blocks[row] = ThinkingBlock(
            row=row,
            content=self._thinking_turn_text.strip("\n"),
        )
        self._latest_thinking_row = row
        self._thinking_segment_active = False
        self._clickables[row] = ("thinking", str(row))
        self.render()

    def begin_thinking_turn(self) -> None:
        self._thinking_turn_text = ""
        self._latest_thinking_row = None
        self._thinking_segment_active = False

    def append_thinking_delta(self, text: str) -> None:
        if not text:
            return
        if self._latest_thinking_row is not None:
            if not self._thinking_segment_active:
                self._thinking_turn_text = (
                    self._thinking_turn_text.rstrip("\n") + "\n\n" + text
                )
                self._thinking_segment_active = True
            else:
                self._thinking_turn_text += text
            block = self._thinking_blocks.get(self._latest_thinking_row)
            if block is not None:
                block.content = self._thinking_turn_text.strip("\n")
                if block.expanded:
                    self.render()
        else:
            self._thinking_turn_text += text

    def toggle_thinking(self, row: int) -> bool:
        block = self._thinking_blocks.get(row)
        if block is None or not block.content.strip():
            return False
        block.expanded = not block.expanded
        if block.expanded:
            self.follow_end = False
            self.scroll_top = self._virtual_row_for_block(row)
        self.render()
        return True

    def toggle_latest_thinking(self) -> bool:
        if self._latest_thinking_row is None:
            return False
        return self.toggle_thinking(self._latest_thinking_row)

    def _virtual_row_for_block(self, row: int) -> int:
        offset = 0
        for earlier_row, block in self._thinking_blocks.items():
            if earlier_row >= row or not block.expanded:
                continue
            rendered = render_markdown(block.content, self._color)
            offset += len(rendered.splitlines())
        return max(0, row + offset)

    def _mark_current(self, kind: str, value: str) -> None:
        if self._pending:
            self._flush_pending()
        row = len(self.transcript) - 1
        if row >= 0:
            self._clickables[row] = (kind, value)
            self.render()

    def write_transcript(self, text: str) -> None:
        if not text:
            return
        remaining = text
        while remaining:
            match = _REWRITE_RE.search(remaining)
            if match is not None:
                before = remaining[:match.start()]
                self._append_fragment(before, commit=False)
                self._pending = ""
                remaining = remaining[match.end():]
                continue
            if "\r" in remaining and "\n" not in remaining:
                parts = remaining.split("\r")
                self._pending = parts[-1]
                remaining = ""
                continue
            self._append_fragment(remaining, commit=True)
            remaining = ""
        if self.follow_end:
            self.scroll_to_end(render=False)
        self.render()

    def _append_fragment(self, text: str, *, commit: bool) -> None:
        if not commit:
            self._pending += text
            return
        combined = self._pending + text
        parts = combined.split("\n")
        self._pending = parts.pop() if parts else ""
        for part in parts:
            self.transcript.append(part)

    def _flush_pending(self) -> None:
        if self._pending:
            self.transcript.append(self._pending)
            self._pending = ""

    def set_panels(self, lines: tuple[str, ...] | list[str]) -> None:
        self.panels = tuple(lines)
        self.render()

    def set_status(self, status: str) -> None:
        self.status = " ".join(status.split())
        self.render()

    def set_input(
        self,
        text: str,
        suggestions: tuple[str, ...] = (),
        *,
        cursor_row: int = 0,
        cursor_column: int = 0,
        visible: bool = True,
    ) -> None:
        self.input_text = text
        self.suggestions = suggestions
        self.input_cursor_row = max(0, cursor_row)
        self.input_cursor_column = max(0, cursor_column)
        self.input_visible = visible
        self.input_rows = max(1, len(text.split("\n")))
        self.render()

    def hide_input(self) -> None:
        self.input_visible = False
        self.render()

    def scroll_by(self, lines: int) -> None:
        content = self._content_lines()
        if not content:
            return
        max_top = max(0, len(content) - self._viewport_height)
        self.scroll_top = max(0, min(max_top, self.scroll_top + lines))
        self.follow_end = self.scroll_top >= max_top
        self.render()

    def scroll_to_end(self, *, render: bool = True) -> None:
        self.scroll_top = max(
            0, len(self._content_lines()) - self._viewport_height)
        self.follow_end = True
        if render:
            self.render()

    def handle_viewport_key(self, key: str) -> bool:
        page = max(1, self._viewport_height - 1)
        if key == "pageup":
            self.scroll_by(-page)
            return True
        if key == "pagedown":
            self.scroll_by(page)
            return True
        if key == "home":
            self.scroll_top = 0
            self.follow_end = False
            self.render()
            return True
        if key == "end":
            self.scroll_to_end()
            return True
        return False

    def handle_mouse(self, event: MouseEvent) -> bool:
        if event.button & 64:
            self.scroll_by(-3 if event.button & 1 == 0 else 3)
            return True
        if event.y - 1 >= self._viewport_height:
            return True
        if event.button & 32:
            if self._selection_anchor is not None:
                self._selection_focus = self._point_for_event(event)
                self._selection_dragged = True
                self.render()
            return True
        if event.is_left_click:
            point = self._point_for_event(event)
            self._selection_anchor = point
            self._selection_focus = point
            self._selection_dragged = False
            return True
        if not event.pressed and event.button & 3 == 0:
            if self._selection_anchor is None:
                return True
            self._selection_focus = self._point_for_event(event)
            if self._selection_dragged:
                self._copy_selection()
            else:
                self._activate_click(event)
            self._selection_anchor = None
            self._selection_focus = None
            self._selection_dragged = False
            self.render()
            return True
        return True

    def _point_for_event(self, event: MouseEvent) -> SelectionPoint:
        row = max(0, min(
            self._viewport_height - 1,
            event.y - 1,
        ))
        return SelectionPoint(
            row=self.scroll_top + row,
            column=max(0, event.x - 1),
        )

    def _activate_click(self, event: MouseEvent) -> None:
        screen_row = max(0, min(
            self._viewport_height - 1, event.y - 1))
        target = self._visible_clickables.get(screen_row)
        if target and self._on_click is not None:
            self._on_click(target[0], target[1])
            if target[0] == "tool":
                for row, value in tuple(self._clickables.items()):
                    if value == target:
                        self._clickables.pop(row, None)

    def _copy_selection(self) -> None:
        if (
            self._selection_anchor is None
            or self._selection_focus is None
        ):
            return
        start, end = sorted(
            (self._selection_anchor, self._selection_focus),
            key=lambda point: (point.row, point.column),
        )
        if start.row == end.row and start.column == end.column:
            return
        selected: list[str] = []
        for row in range(start.row, end.row + 1):
            content = self._content_lines()
            line = strip_ansi(content[row]) if row < len(content) else ""
            lo = start.column if row == start.row else 0
            hi = end.column + 1 if row == end.row else len(line)
            selected.append(line[lo:hi])
        text = "\n".join(selected)
        if not text:
            return
        copied = self._clipboard(text)
        if not copied:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            self._terminal_write(f"\x1b]52;c;{encoded}\x07")
        self.write_transcript("  ⎿ Copied!\n")

    def render(self, *, force: bool = False) -> None:
        if not self._active:
            return
        columns, rows = shutil.get_terminal_size((80, 24))
        self._viewport_width = max(1, columns)
        bottom = self._bottom_lines(columns)
        self._viewport_height = max(1, rows - len(bottom))
        content, clickables = self._render_content()
        max_top = max(0, len(content) - self._viewport_height)
        if self.follow_end:
            self.scroll_top = max_top
        else:
            self.scroll_top = max(0, min(self.scroll_top, max_top))
        viewport = content[
            self.scroll_top:self.scroll_top + self._viewport_height]
        self._visible_clickables = {
            row - self.scroll_top: target
            for row, target in clickables.items()
            if self.scroll_top <= row < self.scroll_top + self._viewport_height
        }
        while len(viewport) < self._viewport_height:
            viewport.append("")
        screen = [
            self._selection_line(line, self.scroll_top + index, columns)
            for index, line in enumerate(viewport)
        ]
        screen.extend(fit_line(line, columns) for line in bottom)
        output = SYNC_BEGIN + CLEAR_SCREEN + "\n".join(screen)
        cursor_row = self._viewport_height + self._input_cursor_screen_row()
        cursor_column = min(
            columns,
            self.input_cursor_column + _display_width(self._prompt_prefix()),
        )
        output += (
            f"\x1b[{max(1, cursor_row + 1)};{max(1, cursor_column + 1)}H"
            + (SHOW_CURSOR if self.input_visible else HIDE_CURSOR)
            + SYNC_END
        )
        if force or output != self._last_render:
            self._last_render = output
            self._terminal_write(output)

    def _selection_line(
        self,
        line: str,
        content_row: int,
        columns: int,
    ) -> str:
        if (
            self._selection_anchor is None
            or self._selection_focus is None
        ):
            return fit_line(line, columns)
        start, end = sorted(
            (self._selection_anchor, self._selection_focus),
            key=lambda point: (point.row, point.column),
        )
        if start.row <= content_row <= end.row:
            return f"\x1b[7m{fit_line(line, columns)}\x1b[27m"
        return fit_line(line, columns)

    def _prompt_prefix(self) -> str:
        return ""

    def _input_cursor_screen_row(self) -> int:
        return (
            len(self.panels)
            + 1
            + self.input_cursor_row
        )

    def _bottom_lines(self, columns: int) -> list[str]:
        if not self.input_visible:
            return []
        rule = "─" * max(1, columns - 1)
        if self._color:
            rule = f"{DIM}{rule}{RESET}"
        source = self.input_text.split("\n")
        lines = [source[0]]
        lines.extend("  " + line for line in source[1:])
        result = [*self.panels, rule, *lines, rule]
        if self.status:
            status = f"  {self.status}"
            if self._color:
                status = f"{DIM}{status}{RESET}"
            result.append(status)
        rows = shutil.get_terminal_size((80, 24)).lines
        max_bottom = min(14, max(6, rows // 2))
        result.extend(self.suggestions[:max(0, max_bottom - len(result))])
        return result[-max_bottom:]


class AltScreenPrompt(PromptWriter):
    """PromptWriter-compatible facade backed by :class:`AltScreenSurface`."""

    def __init__(self, surface: AltScreenSurface, *, color: bool) -> None:
        super().__init__(enabled=False, color=color)
        self.surface = surface
        self._alt_text = ""
        self._alt_visible = False

    @property
    def cursor_row_relative(self) -> int:
        return self.surface.input_cursor_row + len(self.surface.panels) + 1

    @property
    def rendered_rows(self) -> int:
        return len(self.surface._bottom_lines(shutil.get_terminal_size((80, 24)).columns))

    def show(self, text: str) -> None:
        self._alt_text = text
        self._alt_visible = True
        self.surface.set_input(
            text,
            cursor_row=max(0, len(text.split("\n")) - 1),
            cursor_column=_display_width(text.split("\n")[-1]),
            visible=True,
        )

    def update(
        self,
        text: str,
        suggestions: Optional[list[str]] = None,
        *,
        cursor_row: Optional[int] = None,
        cursor_column: Optional[int] = None,
    ) -> None:
        self._alt_text = text
        self._alt_visible = True
        lines = text.split("\n")
        row = len(lines) - 1 if cursor_row is None else cursor_row
        column = (
            _display_width(lines[row])
            if cursor_column is None else cursor_column
        )
        self.surface.set_input(
            text,
            tuple(suggestions or ()),
            cursor_row=row,
            cursor_column=column,
            visible=True,
        )

    def set_panels(self, lines: list[str] | tuple[str, ...], *, redraw: bool = True) -> None:
        self.surface.set_panels(lines)

    def set_status(self, status: str) -> None:
        self.surface.set_status(status)

    def suspend(self) -> None:
        self._alt_visible = False
        self.surface.hide_input()

    def hide(self) -> None:
        self.suspend()

    def resume_or_show(self, fallback: str) -> None:
        if not self._alt_visible:
            text = self._alt_text or fallback
            self.show(text)

    def retarget_prompt(self, old: str, new: str) -> None:
        if self._alt_text.startswith(old):
            self._alt_text = new + self._alt_text[len(old):]
            if self._alt_visible:
                self.show(self._alt_text)

    def submitted(self, *, raw: bool = True) -> None:
        submitted = self._alt_text.rstrip()
        if raw and submitted:
            self.surface.write_transcript(submitted + "\n")
        self._alt_text = ""
        self.hide()

    def finish(self) -> None:
        self.hide()


__all__ = ["AltScreenPrompt", "AltScreenSurface", "strip_ansi", "visible_width"]
