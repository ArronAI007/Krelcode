"""SGR mouse and cursor-position escape sequence helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

MOUSE_ENABLE = "\x1b[?1000h\x1b[?1006h"
MOUSE_DISABLE = "\x1b[?1006l\x1b[?1000l"
CURSOR_POSITION_QUERY = "\x1b[6n"

_SGR_MOUSE_RE = re.compile(r"^\[<(\d+);(\d+);(\d+)([Mm])$")
_CURSOR_POSITION_RE = re.compile(r"^\[(\d+);(\d+)R$")


@dataclass(frozen=True)
class MouseEvent:
    """A decoded SGR mouse event using 1-based terminal coordinates."""

    button: int
    x: int
    y: int
    pressed: bool
    modifiers: frozenset[str] = frozenset()

    @property
    def is_left_click(self) -> bool:
        return self.pressed and (self.button & 0b11) == 0


def parse_sgr_mouse(sequence: bytes | str) -> Optional[MouseEvent]:
    """Decode ``ESC[<button;x;yM/m``.

    The leading ESC may be present.  The mouse button field follows xterm's
    SGR encoding; modifier bits are retained for future bindings.
    """
    if isinstance(sequence, bytes):
        try:
            text = sequence.decode("ascii")
        except UnicodeDecodeError:
            return None
    else:
        text = sequence
    if text.startswith("\x1b"):
        text = text[1:]
    match = _SGR_MOUSE_RE.fullmatch(text)
    if match is None:
        return None
    button = int(match.group(1))
    x = int(match.group(2))
    y = int(match.group(3))
    if x <= 0 or y <= 0:
        return None
    modifiers: set[str] = set()
    if button & 4:
        modifiers.add("shift")
    if button & 8:
        modifiers.add("alt")
    if button & 16:
        modifiers.add("ctrl")
    return MouseEvent(
        button=button,
        x=x,
        y=y,
        pressed=match.group(4) == "M",
        modifiers=frozenset(modifiers),
    )


def parse_cursor_position(sequence: bytes | str) -> Optional[tuple[int, int]]:
    """Decode a CPR/DSR response ``ESC[row;columnR`` as 1-based values."""
    if isinstance(sequence, bytes):
        try:
            text = sequence.decode("ascii")
        except UnicodeDecodeError:
            return None
    else:
        text = sequence
    if text.startswith("\x1b"):
        text = text[1:]
    match = _CURSOR_POSITION_RE.fullmatch(text)
    if match is None:
        return None
    row, column = int(match.group(1)), int(match.group(2))
    if row <= 0 or column <= 0:
        return None
    return row, column
