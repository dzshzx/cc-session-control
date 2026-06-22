"""RC view — Remote Control management with urwid ListBox."""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..actions.rc_ops import start_all_listed, start_project, stop_all_rc, stop_project, toggle_autostart
from ..data.rc import set_rc_at_startup
from ..models import RCProject

if TYPE_CHECKING:
    from ..app import App


class RCRow(urwid.WidgetWrap):
    def __init__(self, project: RCProject) -> None:
        self.project = project
        status_map = {"running": "● 运行中", "dead": "✖ 已崩溃", "stopped": "○ 已停止"}
        status_text = status_map.get(project.status, project.status)
        auto = "✓" if project.auto_start else "✗"
        rc = "✓" if project.rc_at_startup is not False else "✗"
        name = project.name if project.in_list or project.status == "running" else f"({project.name})"

        cols = urwid.Columns([
            (10, urwid.Text(status_text)),
            (4, urwid.Text(auto, align="center")),
            (4, urwid.Text(rc, align="center")),
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
        self._loaded = False

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        body = urwid.AttrMap(self.listbox, {None: "body"})
        self.widget = urwid.Frame(body, header=self.status)

    def keyhints(self) -> str:
        return "Enter 启动 · s 停止 · a 自启 · c 接管 · A 全启 · S 全停"

    def load(self) -> None:
        from ..data.rc import scan
        self._projects = scan()
        self._loaded = True
        self._rebuild()

    def set_pending(self, projects: list[RCProject]) -> None:
        self._pending = projects

    def apply_data(self) -> None:
        if self._pending is not None:
            self._projects = self._pending
            self._pending = None
            self._loaded = True
            self._rebuild()

    def _rebuild(self) -> None:
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        for p in self._projects:
            self.walker.append(RCRow(p))
        if self.walker and focus_pos is not None:
            self.walker.set_focus(min(focus_pos, len(self.walker) - 1))
        running = sum(1 for p in self._projects if p.status == "running")
        auto = sum(1 for p in self._projects if p.auto_start)
        rc_off = sum(1 for p in self._projects if p.rc_at_startup is False)
        rc_text = f" · 接管关 {rc_off}" if rc_off else ""
        self.status.original_widget.set_text(f" 共 {len(self._projects)} 项目 · 运行 {running} · 自启 {auto}{rc_text}")

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
            self.app.trigger_async_refresh()
        elif key == "s" and p:
            ok = stop_project(p.name)
            self.app.notify(f"已停止 {p.name}" if ok else "未在运行")
            self.app.trigger_async_refresh()
        elif key == "a" and p:
            new = toggle_autostart(p.name)
            self.app.notify(f"{p.name} 自启: {'开' if new else '关'}")
            self.app.trigger_async_refresh()
        elif key == "c" and p:
            current = p.rc_at_startup is not False
            set_rc_at_startup(p.directory, not current if current else None)
            label = "关" if current else "开"
            self.app.notify(f"{p.name} 会话接管: {label}")
            self.app.trigger_async_refresh()
        elif key == "A":
            count = start_all_listed()
            self.app.notify(f"已启动 {count} 个项目")
            self.app.trigger_async_refresh()
        elif key == "S":
            ok = stop_all_rc()
            self.app.notify("已停止全部" if ok else "本来就没在跑")
            self.app.trigger_async_refresh()
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
