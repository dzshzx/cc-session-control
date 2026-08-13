"""RC-tab row widgets and their shared status maps (split out of `rc.py`
for the 600-line budget, same discipline as `_session_row.py`).

The status maps are shared by the project rows (in `rc.py`) and the server
rows (here): one vocabulary, one attr mapping, one focus map — never two.
"""

from __future__ import annotations

import urwid

from ..models import RCServer
from ._colspec import ColSpec, row_columns

STATUS_MAP = {
    "running": "● 运行中",
    "dead": "✖ 已退出",
    "stopped": "○ 已停止",
    "unknown": "？ 未知",
}
# Row attr per server/project status — dead (crashed pane) is a semantic error
# state and gets its own red entry (shape ✖ + word 已退出 + color: 3 channels).
STATUS_ATTR = {
    "running": "alive",
    "dead": "status_err",
    "stopped": "dead",
    "unknown": "status_err",
}
RC_FOCUS = {
    "alive": "selected",
    "status_err": "selected",
    "dead": "selected",
    None: "selected",
}


class DividerRow(urwid.WidgetWrap):
    """Non-selectable section separator (focus skips it)."""

    def __init__(self, text: str) -> None:
        super().__init__(urwid.AttrMap(urwid.Text(text), "col_header"))

    def selectable(self) -> bool:
        return False


class ServerRow(urwid.WidgetWrap):
    """A project RC server (managed/external) — display only, never actionable."""

    _COLS: list[ColSpec] = [
        (10, "left", ""),
        (8, "left", ""),
        (8, "right", ""),
        (("weight", 2), "left", ""),
        (("weight", 3), "left", ""),
    ]

    def __init__(self, server: RCServer) -> None:
        self.server = server
        status_text = STATUS_MAP.get(server.status, server.status)
        badge = "托管" if server.managed else "外部"
        pid = str(server.pid) if server.pid else "-"
        cols = row_columns(
            self._COLS,
            [
                status_text,
                badge,
                pid,
                server.name,
                server.cwd or "",
            ],
        )
        attr = STATUS_ATTR.get(server.status, "dead")
        mapped = urwid.AttrMap(cols, attr, focus_map=RC_FOCUS)
        super().__init__(mapped)

    def selectable(self) -> bool:
        # P4: display-only — focus SKIPS it (like DividerRow) so the user never
        # lands on a highlighted row whose keys are all silently inert.
        return False

    def keypress(self, size: tuple, key: str) -> str | None:
        return key
