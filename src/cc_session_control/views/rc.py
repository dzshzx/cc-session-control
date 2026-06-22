"""RC Tab — Remote Control management with DataTable."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, Static

from ..actions.rc_ops import start_all_listed, start_project, stop_all_rc, stop_project, toggle_autostart
from ..data.rc import scan
from ..models import RCProject


class RCView(Container):
    BINDINGS = [
        Binding("enter", "start", "Start", show=True),
        Binding("s", "stop", "Stop", show=True),
        Binding("a", "toggle_auto", "Toggle auto", show=True),
        Binding("shift+a", "start_all", "Start all", show=True),
        Binding("shift+s", "stop_all", "Stop all", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._projects: list[RCProject] = []

    def compose(self) -> ComposeResult:
        yield Static("Scanning...", id="rc-status")
        yield DataTable(id="rc-table")

    def on_mount(self) -> None:
        table = self.query_one("#rc-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Status", "Auto", "Project", "Directory")
        self.load_data()

    @work(thread=True)
    def load_data(self) -> None:
        projects = scan()
        self.app.call_from_thread(self._update_table, projects)

    def _update_table(self, projects: list[RCProject]) -> None:
        self._projects = projects
        table = self.query_one("#rc-table", DataTable)
        table.clear()
        for p in projects:
            status_icon = {"running": "● running", "dead": "✖ dead", "stopped": "○ stopped"}.get(p.status, p.status)
            auto = "✓" if p.auto_start else "✗"
            display_name = p.name if p.in_list or p.status == "running" else f"({p.name})"
            table.add_row(status_icon, auto, display_name, p.directory, key=p.name)
        status = self.query_one("#rc-status", Static)
        running = sum(1 for p in projects if p.status == "running")
        auto = sum(1 for p in projects if p.auto_start)
        status.update(f"{len(projects)} projects · {running} running · {auto} auto-start")

    def _selected(self) -> RCProject | None:
        table = self.query_one("#rc-table", DataTable)
        if table.row_count == 0:
            return None
        key = list(table.rows.keys())[table.cursor_row]
        return next((p for p in self._projects if p.name == key.value), None)

    def action_start(self) -> None:
        p = self._selected()
        if not p:
            return
        if not p.trusted:
            self.notify("Not trusted — run 'claude' in that directory first", severity="warning")
            return
        if p.status == "running":
            self.notify("Already running", severity="information")
            return
        ok = start_project(p.name)
        self.notify(f"Started ws/{p.name}" if ok else "Failed to start", severity="information" if ok else "error")
        self.load_data()

    def action_stop(self) -> None:
        p = self._selected()
        if not p:
            return
        ok = stop_project(p.name)
        self.notify(f"Stopped {p.name}" if ok else "Not running")
        self.load_data()

    def action_toggle_auto(self) -> None:
        p = self._selected()
        if not p:
            return
        new_state = toggle_autostart(p.name)
        self.notify(f"{p.name} auto-start: {'on' if new_state else 'off'}")
        self.load_data()

    def action_start_all(self) -> None:
        count = start_all_listed()
        self.notify(f"Started {count} project(s)")
        self.load_data()

    def action_stop_all(self) -> None:
        ok = stop_all_rc()
        self.notify("Stopped all" if ok else "Nothing running")
        self.load_data()

    def action_refresh(self) -> None:
        self.load_data()
