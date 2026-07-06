"""Row widgets + presentation helpers for the Sessions tab.

Split out of `views/sessions.py` so that file stays under the 600-line budget.
Holds the selectable `SessionRow` (with the D9 source badge + the 📱 remote-
control-exposure marker), the cleanup-submenu `_ActionRow`, and the column spec
(read-only overlay text rows live in `_rows.TextRow`). Rows never handle keys — `keypress` returns the key so the
view's single dispatcher sees it (see frontend/widget-patterns.md).
"""

from __future__ import annotations

import time

import urwid

from ..models import Session
from ._colspec import header_columns, row_columns

# Transcript-derived hidden tags -> compact Chinese row marker.
_HIDDEN_MARKERS = {
    "bridge": "桥接",
    "sdk": "SDK",
}

# Coarse registry `source` bucket -> short badge shown in the 来源 column.
_SOURCE_BADGES = {
    "cli": "CLI",
    "vscode": "IDE",
    "sdk": "SDK",
    "bg": "BG",
}

# One spec drives both the header and every row (see _colspec.py). Text columns
# left; the numeric 提问 and the ragged relative 时间 right-align so their line-
# to-line anchor is stable.
SESSION_COLS = [
    (8, "left", "状态"),
    (4, "left", "来源"),
    (4, "left", "远控"),
    (11, "right", "时间"),
    (5, "right", "提问"),
    (("weight", 3), "left", "标题"),
    (("weight", 1), "left", "项目"),
]


def _hidden_marker(session: Session) -> str:
    """Compact `桥接 SDK` label from a session's transcript `hidden` tags."""
    known = [label for key, label in _HIDDEN_MARKERS.items() if key in session.hidden]
    unknown = sorted(key for key in session.hidden if key not in _HIDDEN_MARKERS)
    return " ".join(known + unknown)


def _source_badge(session: Session) -> str:
    """Short source badge (CLI / IDE / SDK / BG), or "" when unknown."""
    return _SOURCE_BADGES.get(session.source, "")


def _flags(session: Session) -> str:
    """Remote-control exposure marker for the 远控 column: 📱 when this session
    exposes its own session-level remote control (phone / claude.ai/code can take
    it over), else "". 📱 is Emoji_Presentation=Yes so its width is stable across
    terminals (the old ⚙ agent glyph was the width-unstable one — text-default,
    needs VS16 — and is the only thing P5 actually needed to drop). Agent-link is
    deliberately NOT shown here: it is orthogonal to remote control and already
    covered by the 来源 `BG` badge plus the dedicated 后台 tab."""
    return "📱" if session.rc_exposed else ""


def _status_parts(session: Session) -> tuple[str, str]:
    """(状态 cell text, row attr) — shape + word + color, three channels.

    Frontend spec: state may not ride on color alone; the word carries the
    meaning (忙 = generating/tool-running, 闲 = waiting for input, 停 = no
    process), the ●/○ shape survives colorless terminals, and only the
    established ●=on / ○=off convention is used (no ◐ — ambiguous, and an
    East-Asian-Ambiguous width risk, the P5 glyph lesson).

    A live tmux-resident session (ADR-0001) additionally shows the ⧉ badge —
    U+29C9 is East_Asian_Width=Neutral (width-stable 1 cell, verified against
    wcwidth + urwid.calc_width, the P5 check), unlike the ambiguous glyphs the
    P5 lesson banned. Data comes from the snapshot's `tmux_target`; the resume
    actions read the SAME field."""
    cur = "▸" if session.current else " "
    if session.alive:
        word = "忙" if session.status == "busy" else "闲"
        badge = " ⧉" if session.tmux_target else ""
        return f"{cur}● {word}{badge}", ("status_busy" if word == "忙" else "alive")
    return f"{cur}○ 停", "dead"


def _rel_time(mtime: float) -> str:
    """Human relative time: 刚刚 / N 分钟前 / N 小时前 / N 天前; falls back to an
    absolute %m-%d date past a week (and for a missing or future mtime)."""
    if not mtime:
        return "-"
    delta = time.time() - mtime
    if delta < 0:
        return time.strftime("%m-%d %H:%M", time.localtime(mtime))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)} 天前"
    return time.strftime("%m-%d %H:%M", time.localtime(mtime))


class SessionRow(urwid.WidgetWrap):
    def __init__(self, session: Session) -> None:
        self.session = session
        status_cell, attr = _status_parts(session)
        when = _rel_time(session.mtime)
        hidden = _hidden_marker(session)
        label = f"[{hidden}] {session.label}" if hidden else session.label
        if len(label) > 80:
            label = label[:79] + "…"
        cwd = session.cwd.rstrip("/").rsplit("/", 1)[-1] if session.cwd else ""

        cols = row_columns(SESSION_COLS, [
            status_cell,
            _source_badge(session),
            _flags(session),
            when,
            f"p{session.prompts}",
            label,
            cwd,
        ])
        mapped = urwid.AttrMap(
            cols, attr,
            focus_map={"status_busy": "selected", "alive": "selected", "dead": "selected", None: "selected"},
        )
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class _ActionRow(urwid.WidgetWrap):
    def __init__(self, action_key: str, label: str, count: int) -> None:
        self.action_key = action_key
        cols = urwid.Columns([
            ("weight", 1, urwid.Text(label)),
            (8, urwid.Text(str(count), align="right")),
        ], dividechars=2)
        mapped = urwid.AttrMap(cols, "dead", focus_map={"dead": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


_SESSION_HEADER = header_columns(SESSION_COLS)
