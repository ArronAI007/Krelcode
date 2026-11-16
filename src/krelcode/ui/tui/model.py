"""Pure data model for the REPL panel dock.

The model deliberately contains no terminal calls or session imports.  A view
snapshot is assembled by the REPL and rendered by :mod:`layout`, which keeps
layout and interaction logic unit-testable without a PTY.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PanelItem:
    """A selectable row inside a panel."""

    id: str
    label: str
    detail: str = ""
    status: str = ""


@dataclass(frozen=True)
class Panel:
    """One panel tab and its ordered items."""

    id: str
    title: str
    items: tuple[PanelItem, ...] = field(default_factory=tuple)
    badge: str = ""
    focusable: bool = True


def unique_panel_items(items: tuple[PanelItem, ...]) -> tuple[PanelItem, ...]:
    """Give duplicate/empty ids stable unique keys for hit testing."""
    seen: dict[str, int] = {}
    result: list[PanelItem] = []
    for index, item in enumerate(items):
        base = item.id or f"item-{index + 1}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        identifier = base if count == 1 else f"{base}-{count}"
        result.append(item if identifier == item.id else PanelItem(
            id=identifier,
            label=item.label,
            detail=item.detail,
            status=item.status,
        ))
    return tuple(result)
