"""Row widgets + presentation helpers for the Sessions tab.

Split out of `views/sessions.py` to keep row presentation independently
readable.
Holds the selectable `SessionRow` (with the D9 source badge + the 📱 remote-
control-exposure marker), the cleanup-submenu `_ActionRow`, and the column spec
(read-only overlay text rows live in `_rows.TextRow`). Rows never handle keys — `keypress` returns the key so the
view's single dispatcher sees it (see frontend/widget-patterns.md).
"""

from __future__ import annotations

import time

import urwid

from ..data import providers
from ..models import Session
from ._colspec import ColSpec, header_columns, row_columns
from ._rows import SelectableRow, dead_mapped, truncate_cells

# Transcript-derived hidden tags -> compact Chinese row marker.
_HIDDEN_MARKERS = {
    "bridge": "桥接",
    "sdk": "SDK",
}

# Coarse registry `source` bucket -> short badge shown in the 来源 column.
_SOURCE_BADGES = {
    "cli": "CLI",
    "vscode": "IDE",
    # Codex Desktop launches (originator tells them apart from real VS Code
    # sessions, whose pipeline Desktop reuses — codex._classify_source).
    "desktop": "桌面",
    "sdk": "SDK",
    "bg": "BG",
    # ChatGPT mobile/remote-launched codex sessions (ADR-0005 provider
    # layer): typically app-server-hosted, often show dead in /proc, so the
    # "CLI" badge would wrongly imply a direct terminal接回 is available.
    "remote": "远程",
}

# One spec drives both the header and every row (see _colspec.py). Text columns
# left; the numeric 提问 and the ragged relative 时间 right-align so their line-
# to-line anchor is stable.
SESSION_COLS: list[ColSpec] = [
    (8, "left", "状态"),
    (3, "left", "CLI"),
    (4, "left", "来源"),
    (4, "left", "远控"),
    (11, "right", "时间"),
    (5, "right", "提问"),
    (("weight", 3), "left", "标题"),
    (("weight", 1), "left", "项目"),
]


def _provider_badge(session: Session) -> str:
    """The owning CLI's short tag (cc / cx / km) — ADR-0005 provider column.

    `providers.get` stays loud on an unknown key: a garbage provider value is
    a programming error, not renderable uncertainty."""
    return providers.get(session.provider).label


def _prompts_cell(session: Session) -> str:
    """`pN` for Claude rows; non-Claude discovery reads rollout/state heads
    only, so the prompt count is UNKNOWN (shown `-`), not zero."""
    if session.provider != "claude":
        return "-"
    return f"p{session.prompts}"


def _hidden_marker(session: Session) -> str:
    """Compact `桥接 SDK` label from a session's transcript `hidden` tags."""
    known = [label for key, label in _HIDDEN_MARKERS.items() if key in session.hidden]
    unknown = sorted(key for key in session.hidden if key not in _HIDDEN_MARKERS)
    return " ".join(known + unknown)


def _archived_marker(session: Session) -> str:
    """`归档` label marker for rows discovered from a provider's archived
    store: the resume family refuses these until un-archived, so the list
    says so up front (the filter haystack includes it too — one marker
    source for both, like `_hidden_marker`)."""
    return "归档" if session.archived else ""


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
    covered by the 来源 `BG` badge."""
    return "📱" if session.rc_exposed else ""


def _status_parts(session: Session) -> tuple[str, str]:
    """(状态 cell text, row attr) — shape + word + color, three channels.

    Frontend spec: state may not ride on color alone; the word carries the
    meaning (忙 = generating/tool-running, 闲 = waiting for input, 活 =
    argv-bound non-Claude live whose busy/idle is unknowable, 停 = no
    process, 未知 = an unbound live cx/km process may hold this session),
    the ●/○/? shape survives colorless terminals, and only the established
    ●=on / ○=off convention is used (no ◐ — ambiguous, and an
    East-Asian-Ambiguous width risk, the P5 glyph lesson).

    A live tmux-resident session (ADR-0001) additionally shows the ⧉ badge —
    U+29C9 is East_Asian_Width=Neutral (width-stable 1 cell, verified against
    wcwidth + urwid.calc_width, the P5 check), unlike the ambiguous glyphs the
    P5 lesson banned. A live session whose inventory is incomplete shows the
    width-stable ASCII `?`, visibly distinct from confirmed bare residency.
    Data comes from the snapshot's tmux fields; the resume actions read the
    SAME evidence. The fourth state reads `Session.unbound_live_hint` (the
    same field the confirm layer reads) and reuses the semantic `status_err`
    warning attr — honest uncertainty, NOT liveness."""
    cur = "▸" if session.current else " "
    if session.hosted:
        return f"{cur}@ 托管", "alive"
    if session.alive:
        if session.provider != "claude":
            # Non-Claude registries carry no busy/idle status — "闲" would
            # claim "waiting for input" about a session that may be mid-task.
            word = "活"
        else:
            word = "忙" if session.status == "busy" else "闲"
        badge = ""
        if session.tmux_target:
            badge = " ⧉"
        elif not session.tmux_inventory_complete:
            badge = " ?"
        return f"{cur}● {word}{badge}", ("status_busy" if word == "忙" else "alive")
    if session.unbound_live_hint:
        return f"{cur}? 未知", "status_err"
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


class SessionRow(SelectableRow):
    def __init__(self, session: Session) -> None:
        self.session = session
        status_cell, attr = _status_parts(session)
        when = _rel_time(session.mtime)
        hidden = _hidden_marker(session)
        label = f"[{hidden}] {session.label}" if hidden else session.label
        archived = _archived_marker(session)
        if archived:
            label = f"[{archived}] {label}"
        label = truncate_cells(label, 80)
        cwd = session.cwd.rstrip("/").rsplit("/", 1)[-1] if session.cwd else ""

        cols = row_columns(
            SESSION_COLS,
            [
                status_cell,
                _provider_badge(session),
                _source_badge(session),
                _flags(session),
                when,
                _prompts_cell(session),
                label,
                cwd,
            ],
        )
        mapped = urwid.AttrMap(
            cols,
            attr,
            focus_map={
                "status_busy": "selected",
                "alive": "selected",
                "status_err": "selected",
                "dead": "selected",
                None: "selected",
            },
        )
        super().__init__(mapped)


class _ActionRow(SelectableRow):
    def __init__(self, action_key: str, label: str, count: int) -> None:
        self.action_key = action_key
        cols = urwid.Columns(
            [
                ("weight", 1, urwid.Text(label)),
                (8, urwid.Text(str(count), align="right")),
            ],
            dividechars=2,
        )
        super().__init__(dead_mapped(cols))


_SESSION_HEADER = header_columns(SESSION_COLS)
