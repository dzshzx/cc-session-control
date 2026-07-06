"""Single-source key tables — the `_colspec` move applied to keys.

Each view declares ONE `KEY_TABLE` (a tuple of `Key` entries, in footer
order); the footer hint string, the help-overlay body, and the list-mode key
dispatch are all generated from it, so a key's four faces (binding, hint,
help, handler) can never drift apart. Keys owned by the App-level footer
prefix (`r` 刷新) still dispatch here but carry `hint=None`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Key:
    """One verb: its bindings, footer hint, help lines, and handler."""

    keys: tuple[str, ...]        # urwid key names, e.g. ("enter", "o")
    hint: str | None             # footer label; None = shown only in help
    handler: str                 # view method name (resolved via getattr)
    help_lines: tuple[str, ...] = ()  # pre-indented help-overlay display lines
    section: str | None = None   # help section this entry lists under
    needs_selection: bool = True  # call handler(_selected()) vs handler()


@dataclass(frozen=True)
class HelpLayout:
    """The prose around the generated per-key help lines."""

    prefix: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()   # section titles, in display order
    suffix: tuple[str, ...] = ()


def footer_hints(table: tuple[Key, ...]) -> str:
    """The list-mode footer string, straight from the table order."""
    return " · ".join(e.hint for e in table if e.hint)


def help_lines(table: tuple[Key, ...], layout: HelpLayout) -> list[str]:
    """The help-overlay body: prefix, then each section's entries, then suffix."""
    lines = list(layout.prefix)
    for title in layout.sections:
        lines.append(title)
        for e in table:
            if e.section == title:
                lines.extend(e.help_lines)
        lines.append("")
    lines.extend(layout.suffix)
    return lines
