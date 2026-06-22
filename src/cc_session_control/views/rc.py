"""RC view — Remote Control management with urwid ListBox."""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..actions.rc_ops import start_all_listed, start_project, stop_all_rc, stop_project, toggle_autostart
from ..data.rc import scan
from ..models import RCProject

if TYPE_CHECKING:
    from ..app import App


class RCRow(urwid.WidgetWrap):
    def __init__(self, project: RCProject) -> None:
        self.project = project
        status_map = {"running": "● 运行中", "dead": "✖ 已崩溃", "stopped": "○ 已停止"}
        status_text = status_map.get(project.status, project.status)
        auto = "✓" if project.auto_start else "✗"
        name = project.name if project.in_list or project.status == "running" else f"({project.name})"

        cols = urwid.Columns([
            (10, urwid.Text(status_text)),
            (4, urwid.Text(auto, align="center")),
            ("weight", 1, urwid.Text(name, wrap="clip")),
            ("weight", 1, urwid.Text(project.directory, wrap="clip")),
        ], min_width=6)

        attr = "rc_running" if project.status == "running" else "rc_stopped"
        mapped = urwid.AttrMap(cols, attr, focus_map={"rc_running": "selected", "rc_stopped": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class RCView:
    def __init__(self, app: App) -> None:
        self.app = app
        self._projects: list[RCProject] = []
        self._pending: list[RCProject] | None = None

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        self.widget = urwid.Frame(self.listbox, header=self.status)

    def keyhints(self) -> str:
        return "Enter 启动 · s 停止 · a 切换自启 · A 全部启动 · S 全部停止"

    def load(self) -> None:
        self._projects = scan()
        self._rebuild()

    def refresh_data(self) -> None:
        self._pending = scan()

    def apply_data(self) -> None:
        if self._pending is not None:
            self._projects = self._pending
            self._pending = None
            self._rebuild()

    def _rebuild(self) -> None:
        self.walker.clear()
        for p in self._projects:
            self.walker.append(RCRow(p))
        running = sum(1 for p in self._projects if p.status == "running")
        auto = sum(1 for p in self._projects if p.auto_start)
        self.status.original_widget.set_text(f" 共 {len(self._projects)} 项目 · 运行 {running} · 自启 {auto}")

    def _selected(self) -> RCProject | None:
        if not self.walker:
            return None
        widget = self.walker.get_focus()[0]
        if isinstance(widget, RCRow):
            return widget.project
        return None

    def handle_key(self, key: str) -> None:
        p = self._selected()

        if key == "enter" and p:
            if not p.trusted:
                self.app.notify("未信任 — 先在该目录跑一次 claude")
                return
            if p.status == "running":
                self.app.notify("已在运行")
                return
            ok = start_project(p.name)
            self.app.notify(f"已启动 ws/{p.name}" if ok else "启动失败")
            self.load()
        elif key == "s" and p:
            ok = stop_project(p.name)
            self.app.notify(f"已停止 {p.name}" if ok else "未在运行")
            self.load()
        elif key == "a" and p:
            new = toggle_autostart(p.name)
            self.app.notify(f"{p.name} 自启: {'开' if new else '关'}")
            self.load()
        elif key == "A":
            count = start_all_listed()
            self.app.notify(f"已启动 {count} 个项目")
            self.load()
        elif key == "S":
            ok = stop_all_rc()
            self.app.notify("已停止全部" if ok else "本来就没在跑")
            self.load()
        elif key == "r":
            self.load()
            self.app.notify("已刷新")
