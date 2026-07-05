"""Single-source column specs for the tab tables (frontend spec: one scale).

Each tab declares ONE spec — `(sizing, align, header)` per column — and builds
both its header row and its data rows from it, so the two can never drift
(widget-patterns.md checklist #4: header widths must match row widths; that
drift caused several past fixes).

`sizing` is an int (fixed width in terminal cells ≈ ch) or `("weight", n)`.
Alignment follows the user's frontend spec: text left, numeric/time columns
right; data cells are never centered. `GUTTER` is the uniform inter-column
whitespace (Layout: group with whitespace, not divider lines) — the spacing
scale slice of the spec is unpublished, so 2 cells is a general-best-practice
placeholder, not a spec-mandated value.
"""

from __future__ import annotations

import urwid

ColSpec = tuple[int | tuple[str, int], str, str]

GUTTER = 2


def _sized(sizing: int | tuple[str, int], widget: urwid.Widget):
    if isinstance(sizing, tuple):
        return (*sizing, widget)
    return (sizing, widget)


def header_columns(spec: list[ColSpec]) -> urwid.Columns:
    """The column-header row for a spec (headers inherit the column align)."""
    return urwid.Columns(
        [_sized(sizing, urwid.Text(header, align=align)) for sizing, align, header in spec],
        dividechars=GUTTER,
        min_width=4,
    )


def row_columns(spec: list[ColSpec], cells: list[str]) -> urwid.Columns:
    """A data row for a spec; `cells` are plain strings, one per column."""
    return urwid.Columns(
        [
            _sized(sizing, urwid.Text(cell, align=align, wrap="clip"))
            for (sizing, align, _), cell in zip(spec, cells)
        ],
        dividechars=GUTTER,
        min_width=4,
    )
