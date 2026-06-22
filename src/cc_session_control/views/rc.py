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
        Binding("enter", "start", "启动", show=True),
        Binding("s", "stop", "停止", show=True),
        Binding("a", "toggle_auto", "切换自启", show=True),
        Binding("shift+a", "start_all", "全部启动", show=True),
        Binding("shift+s", "stop_all", "全部停止", show=True),
        Binding("r", "refresh", "刷新", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._projects: list[RCProject] = []

    def compose(self) -> ComposeResult:
        yield Static("扫描中…", id="rc-status")
        yield DataTable(id="rc-table")

    def on_mount(self) -> None:
        table = self.query_one("#rc-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("状态", "自启", "项目", "目录")
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
            status_icon = {"running": "● 运行中", "dead": "✖ 已崩溃", "stopped": "○ 已停止"}.get(p.status, p.status)
            auto = "✓" if p.auto_start else "✗"
            display_name = p.name if p.in_list or p.status == "running" else f"({p.name})"
            table.add_row(status_icon, auto, display_name, p.directory, key=p.name)
        status = self.query_one("#rc-status", Static)
        running = sum(1 for p in projects if p.status == "running")
        auto = sum(1 for p in projects if p.auto_start)
        status.update(f"共 {len(projects)} 项目 · 运行 {running} · 自启 {auto}")

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
            self.notify("未信任 — 先在该目录跑一次 claude", severity="warning")
            return
        if p.status == "running":
            self.notify("已在运行", severity="information")
            return
        ok = start_project(p.name)
        self.notify(f"已启动 ws/{p.name}" if ok else "启动失败", severity="information" if ok else "error")
        self.load_data()

    def action_stop(self) -> None:
        p = self._selected()
        if not p:
            return
        ok = stop_project(p.name)
        self.notify(f"已停止 {p.name}" if ok else "未在运行")
        self.load_data()

    def action_toggle_auto(self) -> None:
        p = self._selected()
        if not p:
            return
        new_state = toggle_autostart(p.name)
        self.notify(f"{p.name} 自启: {'开' if new_state else '关'}")
        self.load_data()

    def action_start_all(self) -> None:
        count = start_all_listed()
        self.notify(f"已启动 {count} 个项目")
        self.load_data()

    def action_stop_all(self) -> None:
        ok = stop_all_rc()
        self.notify("已停止全部" if ok else "本来就没在跑")
        self.load_data()

    def action_refresh(self) -> None:
        self.load_data()
