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


def _cell_prefix(text: str, max_cells: int) -> str:
    if max_cells <= 0:
        return ""
    position, _used = calc_text_pos(text, 0, len(text), max_cells)
    return text[:position]


def truncate_cells(
    text: str,
    width: int,
    marker: str = "…",
) -> str:
    """Truncate text at an urwid cell boundary, without splitting a glyph.

    A marker wider than the available width is itself shortened at a terminal
    cluster boundary; width zero (or less) always produces an empty string.
    """
    if width <= 0:
        return ""
    if calc_width(text, 0, len(text)) <= width:
        return text
    marker_width = calc_width(marker, 0, len(marker))
    if marker_width > width:
        return _cell_prefix(marker, width)
    return _cell_prefix(text, width - marker_width) + marker


class TextRow(urwid.WidgetWrap):
    def __init__(self, text: str) -> None:
        mapped = urwid.AttrMap(
            urwid.Text(text), "dead", focus_map={"dead": "selected", None: "selected"}
        )
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key
