"""RC view — Remote Control management with urwid ListBox."""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..data import rc
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
            (10, urwid.Text(auto, align="center")),
            (10, urwid.Text(rc, align="center")),
            ("weight", 2, urwid.Text(name, wrap="clip")),
            ("weight", 3, urwid.Text(project.directory, wrap="clip")),
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
        self._help = False

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        col_header = urwid.AttrMap(urwid.Columns([
            (10, urwid.Text("状态")),
            (10, urwid.Text("开机自启", align="center")),
            (10, urwid.Text("自动远控", align="center")),
            ("weight", 2, urwid.Text("项目")),
            ("weight", 3, urwid.Text("目录")),
        ], min_width=6), "col_header")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        body = urwid.AttrMap(self.listbox, {None: "body"})
        self.widget = urwid.Frame(body, header=col_header, footer=self.status)

    def keyhints(self) -> str:
        if self._help:
            return "按任意键返回"
        return "Enter 启动 · s 停止 · a 切换开机自启 · c 切换自动远控 · ? 帮助"

    def load(self) -> None:
        from ..data.rc import scan
        self._projects = scan()
        self._loaded = True
        self._rebuild()

    def fetch_pending(self) -> None:
        """Worker-thread data fetch. Only sets pending fields — no widgets."""
        self.set_pending(rc.scan())

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
        rc_text = f" · 自动远控关 {rc_off}" if rc_off else ""
        self.status.original_widget.set_text(f" 共 {len(self._projects)} 项目 · 运行 {running} · 开机自启 {auto}{rc_text}")

    def _selected(self) -> RCProject | None:
        if not self.walker:
            return None
        widget = self.walker.get_focus()[0]
        if isinstance(widget, RCRow):
            return widget.project
        return None

    def _update_footer(self) -> None:
        if self.app.views[self.app._active] is not self:
            return
        hints = self.keyhints()
        self.app.footer_text.set_text(f" Tab 切换 · q 退出 · {hints}")

    def handle_key(self, key: str) -> None:
        if self._help:
            self._help = False
            self._rebuild()
            self._update_footer()
            return

        p = self._selected()

        if key == "enter" and p:
            if not p.trusted:
                self.app.notify("未信任 — 先在该目录跑一次 claude")
                return
            if p.status == "running":
                self.app.notify("已在运行")
                return
            ok = rc.start_one(p.name)
            self.app.notify(f"已启动 ws/{p.name}" if ok else "启动失败")
            self.app.trigger_async_refresh()
        elif key == "s" and p:
            ok = rc.stop_one(p.name)
            self.app.notify(f"已停止 {p.name}" if ok else "未在运行")
            self.app.trigger_async_refresh()
        elif key == "a" and p:
            new = rc.toggle_autostart(p.name)
            self.app.notify(f"{p.name} 开机自启: {'开' if new else '关'}")
            self.app.trigger_async_refresh()
        elif key == "c" and p:
            current = p.rc_at_startup is not False
            set_rc_at_startup(p.directory, not current if current else None)
            label = "关" if current else "开"
            self.app.notify(f"{p.name} 自动远控: {label}")
            self.app.trigger_async_refresh()
        elif key == "A":
            count = rc.start_all_listed()
            self.app.notify(f"已启动 {count} 个项目")
            self.app.trigger_async_refresh()
        elif key == "S":
            ok = rc.stop_all()
            self.app.notify("已停止全部" if ok else "本来就没在跑")
            self.app.trigger_async_refresh()
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
        elif key == "?":
            self._help = True
            lines = [
                "远程控制操作:",
                "  Enter  启动选中项目的远程控制服务",
                "  s      停止选中项目的远程控制服务",
                "  a      切换「开机自启」：A 键一键启动时是否带上本项目",
                "  c      切换「自动远控」：claude 启动时自动开远程控制，手机即可接管",
                "",
                "批量操作:",
                "  A      启动所有「开机自启」项目",
                "  S      停止全部远程控制服务",
                "  r      重新扫描刷新",
                "",
                "导航:",
                "  Tab    切换标签页",
                "  q      退出",
            ]
            self.walker.clear()
            for line in lines:
                w = urwid.AttrMap(urwid.Text(line), "dead")
                self.walker.append(w)
            self.status.original_widget.set_text(" 按任意键返回")
            self._update_footer()
