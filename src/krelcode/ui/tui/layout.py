"""Pure panel-dock layout and hitbox generation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from .model import Panel, PanelItem, unique_panel_items

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def display_width(text: str) -> int:
    plain = _ANSI_RE.sub("", text)
    return sum(
        2 if unicodedata.east_asian_width(ch) in "WF" else
        0 if unicodedata.combining(ch) else 1
        for ch in plain
    )


def truncate_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    out: list[str] = []
    used = 0
    for char in text:
        char_width = (
            2 if unicodedata.east_asian_width(char) in "WF" else
            0 if unicodedata.combining(char) else 1
        )
        if used + char_width > width - 1:
            break
        out.append(char)
        used += char_width
    return "".join(out) + "…"


def pad_display(text: str, width: int) -> str:
    clipped = truncate_display(text, width)
    return clipped + " " * max(0, width - display_width(clipped))


@dataclass(frozen=True)
class PanelHitbox:
    """Rectangular click target in zero-based dock coordinates."""

    panel_id: str
    x1: int
    y1: int
    x2: int
    y2: int
    item_id: str = ""

    def contains(self, x: int, y: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2


@dataclass(frozen=True)
class PanelLayout:
    lines: tuple[str, ...]
    hitboxes: tuple[PanelHitbox, ...]
    active_id: str

    @property
    def height(self) -> int:
        return len(self.lines)

    def hit_test(self, x: int, y: int) -> Optional[PanelHitbox]:
        for hitbox in self.hitboxes:
            if hitbox.contains(x, y):
                return hitbox
        return None


def _tab_specs(panels: Iterable[Panel]) -> list[tuple[str, str]]:
    specs = []
    for panel in panels:
        if not panel.focusable:
            continue
        label = f" {panel.title}"
        if panel.badge:
            label += f" {panel.badge}"
        specs.append((panel.id, label + " "))
    return specs


def _top_border(
    specs: list[tuple[str, str]], active_id: str, width: int,
) -> tuple[str, tuple[PanelHitbox, ...]]:
    if width < 2:
        return " " * width, ()
    available = width - 2
    rendered_tabs: list[str] = []
    hitboxes: list[PanelHitbox] = []
    cursor = 1
    for panel_id, label in specs:
        decorated = f"[{label.strip()}]" if panel_id == active_id else label
        if display_width(decorated) > available:
            continue
        rendered_tabs.append(decorated)
        hitboxes.append(PanelHitbox(
            panel_id=panel_id,
            x1=cursor,
            y1=0,
            x2=cursor + display_width(decorated) - 1,
            y2=0,
        ))
        cursor += display_width(decorated) + 1  # separator column
        available -= display_width(decorated) + 1
    if rendered_tabs:
        title = "┬".join(rendered_tabs)
        remaining = width - 2 - display_width(title)
        top = "╭" + title + ("─" * max(0, remaining)) + "╮"
    else:
        top = "╭" + "─" * max(0, width - 2) + "╮"
    return pad_display(top, width), tuple(hitboxes)


def _item_line(
    item: PanelItem,
    *,
    selected: bool,
    focused: bool,
    inner_width: int,
) -> str:
    selector = "›" if focused and selected else " "
    status = f"{item.status} " if item.status else ""
    label = f"{selector} {status}{item.label}"
    if item.detail:
        label += f" · {item.detail}"
    return pad_display(label, inner_width)


def render_panel_dock(
    panels: Iterable[Panel],
    *,
    active_id: str,
    selected_index: int = 0,
    focused: bool = False,
    width: int = 80,
    body_rows: int = 4,
) -> PanelLayout:
    """Render a tabbed, fixed-height dock with clickable tab/item hitboxes."""
    panel_list = tuple(Panel(
        id=p.id,
        title=p.title,
        items=unique_panel_items(p.items),
        badge=p.badge,
        focusable=p.focusable,
    ) for p in panels)
    if not panel_list:
        return PanelLayout(lines=(), hitboxes=(), active_id="")

    width = max(1, width)
    if width < 8:
        active = next(
            (panel for panel in panel_list if panel.id == active_id),
            panel_list[0],
        )
        fallback = [truncate_display(active.title, width)]
        fallback.extend(
            truncate_display(f"{item.status} {item.label}".strip(), width)
            for item in active.items[:body_rows]
        )
        return PanelLayout(
            lines=tuple(fallback),
            hitboxes=(),
            active_id=active.id,
        )
    active_index = next(
        (index for index, panel in enumerate(panel_list) if panel.id == active_id),
        0,
    )
    active = panel_list[active_index]
    specs = _tab_specs(panel_list)
    top, tab_hitboxes = _top_border(specs, active.id, width)
    inner_width = max(1, width - 4)
    items = active.items
    max_items = max(0, body_rows)
    overflow = len(items) > max_items
    visible_slots = max(0, max_items - 1) if overflow else max_items
    start = 0
    if visible_slots and selected_index >= visible_slots:
        start = min(
            selected_index - visible_slots + 1,
            max(0, len(items) - visible_slots),
        )
    shown = items[start:start + visible_slots]

    body: list[str] = []
    item_hitboxes: list[PanelHitbox] = []
    for visible_index, item in enumerate(shown):
        item_index = start + visible_index
        selected = item_index == selected_index
        body.append(
            "│ " + _item_line(
                item,
                selected=selected,
                focused=focused,
                inner_width=inner_width,
            ) + " │"
        )
        item_hitboxes.append(PanelHitbox(
            panel_id=active.id,
            item_id=item.id,
            x1=1,
            y1=1 + visible_index,
            x2=max(1, width - 2),
            y2=1 + visible_index,
        ))
    if overflow:
        label = (
            f"↑{start} ↓{len(items) - start - len(shown)}"
            f" · {selected_index + 1}/{len(items)}"
        )
        body.append("│ " + pad_display(label, inner_width) + " │")
    while len(body) < max_items:
        placeholder = "（空）" if not items and not body else ""
        body.append("│ " + pad_display(placeholder, inner_width) + " │")

    bottom = "╰" + "─" * max(0, width - 2) + "╯"
    return PanelLayout(
        lines=(pad_display(top, width), *body, pad_display(bottom, width)),
        hitboxes=(*tab_hitboxes, *item_hitboxes),
        active_id=active.id,
    )
