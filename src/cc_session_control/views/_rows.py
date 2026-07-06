"""Shared text-display helpers for the views: the overlay text row + cell-width
truncation.

`TextRow` is the read-only line used in overlay lists (help / watch / cleanup
preview). Selectable so the hosting ListBox can scroll it with the arrow keys,
but every key is returned unhandled — the owning view's `handle_key` decides
what closes the overlay. One class serves all three tabs (single source).
"""

from __future__ import annotations

import urwid
from urwid import calc_text_pos, calc_width

# Max display width of the `「name」` slot in confirm messages (see
# frontend/theming-and-input.md's message template).
CONFIRM_NAME_CELLS = 30


def truncate_cells(text: str, max_cells: int) -> str:
    """Truncate `text` to at most `max_cells` terminal cells, appending `…`.

    Cell width, not character count: CJK characters occupy 2 cells, so a
    `[:30]` slice of a Chinese label can be 60 cells wide — this is the
    display-width-correct replacement for those slices."""
    if calc_width(text, 0, len(text)) <= max_cells:
        return text
    pos, _cols = calc_text_pos(text, 0, len(text), max(max_cells - 1, 0))
    return text[:pos] + "…"


class TextRow(urwid.WidgetWrap):
    def __init__(self, text: str) -> None:
        mapped = urwid.AttrMap(urwid.Text(text), "dead", focus_map={"dead": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key
