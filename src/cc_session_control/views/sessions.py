"""Sessions Tab — DataTable with session list and keyboard actions."""

from __future__ import annotations

import time

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, Static

from ..actions.session_ops import do_resume, resume_cmd, terminate_session, to_clipboard
from ..data.sessions import remove_session, scan
from ..models import Session


class SessionsView(Container):
    BINDINGS = [
        Binding("enter", "resume", "Resume", show=True),
        Binding("f", "fork", "Fork resume", show=True),
        Binding("t", "terminate", "Terminate", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("y", "copy_cmd", "Copy cmd", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        yield Static("Scanning...", id="sessions-status")
        yield DataTable(id="sessions-table")

    def on_mount(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "Time", "Prompts", "Title", "Directory")
        self.load_data()

    @work(thread=True)
    def load_data(self) -> None:
        sessions = scan()
        self.app.call_from_thread(self._update_table, sessions)

    def _update_table(self, sessions: list[Session]) -> None:
        self._sessions = sessions
        table = self.query_one("#sessions-table", DataTable)
        table.clear()
        for s in sessions:
            mark = "●" if s.alive else "○"
            cur = "▸" if s.current else " "
            when = time.strftime("%m-%d %H:%M", time.localtime(s.mtime))
            label = s.label[:50] if len(s.label) > 50 else s.label
            cwd = s.cwd.rstrip("/").rsplit("/", 1)[-1] if s.cwd else ""
            table.add_row(f"{cur}{mark}", when, f"p{s.prompts}", label, cwd, key=s.sid)
        status = self.query_one("#sessions-status", Static)
        alive_n = sum(1 for s in sessions if s.alive)
        status.update(f"{len(sessions)} sessions · {alive_n} alive")

    def _selected(self) -> Session | None:
        table = self.query_one("#sessions-table", DataTable)
        if table.row_count == 0:
            return None
        key = list(table.rows.keys())[table.cursor_row]
        return next((s for s in self._sessions if s.sid == key.value), None)

    def action_resume(self) -> None:
        s = self._selected()
        if not s:
            return
        if s.current:
            self.notify("Cannot resume current session", severity="warning")
            return
        self.app.exit(("resume", s, False))

    def action_fork(self) -> None:
        s = self._selected()
        if not s:
            return
        self.app.exit(("resume", s, True))

    def action_terminate(self) -> None:
        s = self._selected()
        if not s:
            return
        if not s.alive:
            self.notify("Session is not alive", severity="warning")
            return
        if s.current:
            self.notify("Cannot terminate current session", severity="warning")
            return
        ok = terminate_session(s)
        self.notify("Terminated" if ok else "Failed to terminate", severity="information" if ok else "error")
        self.load_data()

    def action_delete(self) -> None:
        s = self._selected()
        if not s:
            return
        if s.alive:
            self.notify("Terminate first before deleting", severity="warning")
            return
        remove_session(s)
        self.notify("Deleted")
        self.load_data()

    def action_copy_cmd(self) -> None:
        s = self._selected()
        if not s:
            return
        cmd = resume_cmd(s)
        ok = to_clipboard(cmd)
        self.notify("Copied to clipboard" if ok else f"Copy failed: {cmd}")

    def action_refresh(self) -> None:
        self.load_data()
