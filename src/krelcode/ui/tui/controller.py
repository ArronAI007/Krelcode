"""Focus, selection, keyboard and mouse routing for the panel dock."""

from __future__ import annotations

from typing import Iterable, Optional

from .layout import PanelLayout, render_panel_dock
from .model import Panel
from .mouse import MouseEvent, parse_sgr_mouse


class PanelController:
    """Stateful interaction adapter around the pure panel layout."""

    def __init__(self, panels: Iterable[Panel] = ()) -> None:
        self._panels: tuple[Panel, ...] = tuple(panels)
        self.active_id = self._panels[0].id if self._panels else ""
        self.focused = False
        self._selected: dict[str, int] = {
            panel.id: 0 for panel in self._panels if panel.focusable
        }
        self._origin_row: Optional[int] = None
        self._layout = PanelLayout((), (), self.active_id)

    @property
    def panels(self) -> tuple[Panel, ...]:
        return self._panels

    @property
    def layout(self) -> PanelLayout:
        return self._layout

    @property
    def active_panel(self) -> Optional[Panel]:
        return next(
            (panel for panel in self._panels if panel.id == self.active_id),
            None,
        )

    @property
    def selected_index(self) -> int:
        return self._selected.get(self.active_id, 0)

    def set_panels(self, panels: Iterable[Panel]) -> None:
        """Replace view data while preserving a valid tab and selection."""
        new_panels = tuple(panels)
        old_active = self.active_id
        self._panels = new_panels
        available = {panel.id for panel in new_panels if panel.focusable}
        if not available:
            self.active_id = new_panels[0].id if new_panels else ""
            self.focused = False
        elif old_active not in available:
            self.active_id = next(iter(available))
        for panel in new_panels:
            current = self._selected.get(panel.id, 0)
            self._selected[panel.id] = max(
                0, min(current, max(0, len(panel.items) - 1)))

    def focus(self, panel_id: str = "") -> bool:
        target = panel_id or self.active_id
        panel = next(
            (item for item in self._panels
             if item.id == target and item.focusable),
            None,
        )
        if panel is None:
            return False
        self.active_id = panel.id
        self.focused = True
        return True

    def blur(self) -> None:
        self.focused = False

    def activate_tab(self, panel_id: str) -> bool:
        if not any(
            panel.id == panel_id and panel.focusable
            for panel in self._panels
        ):
            return False
        self.active_id = panel_id
        self.focused = True
        return True

    def switch_panel(self, delta: int) -> bool:
        focusable = [panel for panel in self._panels if panel.focusable]
        if not focusable:
            return False
        current = next(
            (index for index, panel in enumerate(focusable)
             if panel.id == self.active_id),
            0,
        )
        self.active_id = focusable[(current + delta) % len(focusable)].id
        self.focused = True
        return True

    def move_selection(self, delta: int) -> bool:
        panel = self.active_panel
        if panel is None or not panel.items:
            return False
        current = self.selected_index
        next_index = max(0, min(len(panel.items) - 1, current + delta))
        self._selected[panel.id] = next_index
        return next_index != current

    def handle_key(self, key: str) -> bool:
        """Handle a normalised key.  Return true when the editor must redraw."""
        if key == "ctrl-o":
            if not self.focused:
                return self.focus()
            return self.switch_panel(1)
        if not self.focused:
            return False
        if key in ("escape", "enter"):
            self.blur()
            return True
        if key in ("left", "shift-tab"):
            return self.switch_panel(-1)
        if key in ("right", "tab"):
            return self.switch_panel(1)
        if key == "up":
            self.move_selection(-1)
            return True
        if key == "down":
            self.move_selection(1)
            return True
        return True  # modal focus: never leak editing keys into the prompt

    def render(self, width: int) -> PanelLayout:
        self._layout = render_panel_dock(
            self._panels,
            active_id=self.active_id,
            selected_index=self.selected_index,
            focused=self.focused,
            width=width,
        )
        # Keep the last CPR origin even while the editor owns focus.  Mouse
        # clicks are allowed to focus a tab/item directly; clearing the origin
        # here would make the first click from the prompt impossible.
        return self._layout

    @staticmethod
    def key_from_escape(sequence: bytes) -> str:
        if sequence == b"":
            return "escape"
        return {
            b"[A": "up", b"OA": "up",
            b"[B": "down", b"OB": "down",
            b"[C": "right", b"OC": "right",
            b"[D": "left", b"OD": "left",
            b"[Z": "shift-tab",
            b"\t": "tab",
            b"\r": "enter", b"\n": "enter",
        }.get(sequence, "")

    def set_cursor_origin(self, cursor_row: int, relative_cursor_row: int) -> None:
        """Translate a 1-based CPR row into the dock's zero-based top row."""
        self._origin_row = max(0, (cursor_row - 1) - relative_cursor_row)

    def handle_mouse(self, sequence: bytes | str) -> bool:
        """Handle a decoded SGR click; all mouse reports are consumed."""
        event = parse_sgr_mouse(sequence)
        if event is None or not event.is_left_click:
            return event is not None
        if self._origin_row is None:
            return True
        return self.handle_mouse_event(event, origin_row=self._origin_row)

    def handle_mouse_event(self, event: MouseEvent, *, origin_row: int) -> bool:
        if not event.is_left_click:
            return True
        relative_y = event.y - 1 - origin_row
        hit = self._layout.hit_test(event.x - 1, relative_y)
        if hit is None:
            self.focused = False
            return True
        if hit.item_id:
            self.active_id = hit.panel_id
            panel = self.active_panel
            if panel is not None:
                index = next(
                    (i for i, item in enumerate(panel.items)
                     if item.id == hit.item_id),
                    -1,
                )
                if index >= 0:
                    self._selected[panel.id] = index
            self.focused = True
        else:
            self.activate_tab(hit.panel_id)
        return True
