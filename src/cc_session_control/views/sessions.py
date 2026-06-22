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
        Binding("enter", "resume", "接回", show=True),
        Binding("f", "fork", "分叉接回", show=True),
        Binding("t", "terminate", "终止", show=True),
        Binding("d", "delete", "删除", show=True),
        Binding("y", "copy_cmd", "复制命令", show=True),
        Binding("r", "refresh", "刷新", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        yield Static("扫描中…", id="sessions-status")
        yield DataTable(id="sessions-table")

    def on_mount(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "时间", "提问", "标题", "目录")
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
        status.update(f"共 {len(sessions)} 条会话 · 活 {alive_n}")

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
            self.notify("不能接回当前会话", severity="warning")
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
            self.notify("会话不是活的", severity="warning")
            return
        if s.current:
            self.notify("不能终止当前会话", severity="warning")
            return
        ok = terminate_session(s)
        self.notify("已终止" if ok else "终止失败", severity="information" if ok else "error")
        self.load_data()

    def action_delete(self) -> None:
        s = self._selected()
        if not s:
            return
        if s.alive:
            self.notify("活会话不删，先终止", severity="warning")
            return
        remove_session(s)
        self.notify("已删除")
        self.load_data()

    def action_copy_cmd(self) -> None:
        s = self._selected()
        if not s:
            return
        cmd = resume_cmd(s)
        ok = to_clipboard(cmd)
        self.notify("已复制" if ok else f"复制失败: {cmd}")

    def action_refresh(self) -> None:
        self.load_data()
