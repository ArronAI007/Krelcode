"""Terminal UI primitives for the Krelcode REPL panel dock."""

from .controller import PanelController
from .layout import PanelHitbox, PanelLayout, render_panel_dock
from .model import Panel, PanelItem
from .mouse import MouseEvent, parse_cursor_position, parse_sgr_mouse

__all__ = [
    "MouseEvent",
    "Panel",
    "PanelController",
    "PanelHitbox",
    "PanelItem",
    "PanelLayout",
    "parse_cursor_position",
    "parse_sgr_mouse",
    "render_panel_dock",
]
