"""RC view — the 远程控制 tab.

Shows three things for one machine-wide Remote Control surface:
  1. managed projects (RCProject) with the tri-state `remoteControlAtStartup`
     and `remoteControlSpawnMode`, plus the existing start/stop/autostart keys;
  2. project RC servers (RCServer) discovered via tmux ∪ /proc, badged
     managed/external — external servers are READ-ONLY (no takeover/restart key);
  3. the bridge-environment ledger (current vs orphan). Orphans are labelled
     "云端需手动删除": csctl has NO local deregister — deletion is manual on
     claude.ai/code (capability red line / AC9).

Only project rows are actionable. Server and environment rows are display-only,
so no key toggles RC on a running session, takes over an external server, or
deregisters a cloud environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..data import environments, rc
from ..data.rc import set_rc_at_startup
from ..models import BridgeEnv, RCProject, RCServer

if TYPE_CHECKING:
    from ..data.snapshot import WorldSnapshot

    from ..app import App

_STATUS_MAP = {"running": "● 运行中", "dead": "✖ 已崩溃", "stopped": "○ 已停止"}
_RC_TRISTATE = {True: "开", False: "关", None: "未设置"}

# Literal required in the UI by AC9 — the manual-delete red line.
_MANUAL_DELETE = "云端需手动删除"


class RCRow(urwid.WidgetWrap):
    def __init__(self, project: RCProject) -> None:
        self.project = project
        status_text = _STATUS_MAP.get(project.status, project.status)
        auto = "✓" if project.auto_start else "✗"
        rc_at = _RC_TRISTATE.get(project.rc_at_startup, "未设置")
        spawn = project.spawn_mode or "—"
        name = project.name if project.in_list or project.status == "running" else f"({project.name})"

        cols = urwid.Columns([
            (10, urwid.Text(status_text)),
            (8, urwid.Text(auto, align="center")),
            (8, urwid.Text(rc_at, align="center")),
            (10, urwid.Text(spawn, wrap="clip")),
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


class _DividerRow(urwid.WidgetWrap):
    """Non-selectable section separator (focus skips it)."""

    def __init__(self, text: str) -> None:
        super().__init__(urwid.AttrMap(urwid.Text(text), "col_header"))

    def selectable(self) -> bool:
        return False


class ServerRow(urwid.WidgetWrap):
    """A project RC server (managed/external) — display only, never actionable."""

    def __init__(self, server: RCServer) -> None:
        self.server = server
        status_text = _STATUS_MAP.get(server.status, server.status)
        badge = "托管" if server.managed else "外部"
        pid = str(server.pid) if server.pid else "-"
        cols = urwid.Columns([
            (10, urwid.Text(status_text)),
            (8, urwid.Text(badge, align="center")),
            (8, urwid.Text(pid, align="center")),
            ("weight", 2, urwid.Text(server.name, wrap="clip")),
            ("weight", 3, urwid.Text(server.cwd or "", wrap="clip")),
        ], min_width=6)
        attr = "rc_running" if server.status == "running" else "rc_stopped"
        mapped = urwid.AttrMap(cols, attr, focus_map={"rc_running": "selected", "rc_stopped": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class EnvRow(urwid.WidgetWrap):
    """A bridge-environment ledger entry (current/orphan) — display only."""

    def __init__(self, env: BridgeEnv) -> None:
        self.env = env
        if env.status == "current":
            mark = "● 绑定中"
            hint = ""
        else:
            mark = "○ 孤儿"
            hint = _MANUAL_DELETE
        cols = urwid.Columns([
            (10, urwid.Text(mark)),
            ("weight", 2, urwid.Text(env.env_id, wrap="clip")),
            ("weight", 2, urwid.Text(env.bound_sid or "-", wrap="clip")),
            ("weight", 2, urwid.Text(hint, wrap="clip")),
        ], min_width=6)
        attr = "rc_running" if env.status == "current" else "rc_stopped"
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
        self._servers: list[RCServer] = []
        self._current: list[BridgeEnv] = []
        self._orphans: list[BridgeEnv] = []
        self._pending: list[RCProject] | None = None
        self._pending_servers: list[RCServer] | None = None
        self._pending_current: list[BridgeEnv] | None = None
        self._pending_orphans: list[BridgeEnv] | None = None
        self._loaded = False
        self._help = False

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        col_header = urwid.AttrMap(urwid.Columns([
            (10, urwid.Text("状态")),
            (8, urwid.Text("开机自启", align="center")),
            (8, urwid.Text("自动远控", align="center")),
            (10, urwid.Text("启动模式")),
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
        self._projects = rc.scan()
        self._servers, self._current, self._orphans = self._scan_extras()
        self._loaded = True
        self._rebuild()

    def _scan_extras(self) -> tuple[list[RCServer], list[BridgeEnv], list[BridgeEnv]]:
        """Self-fetch the servers + environment ledger (no-snapshot path).

        Uses the alive-gated `observe_live` (not the bridge-truthy `observe`) so a
        zombie session's stale bridge is not shown as a current/bound env (R3/R6).
        """
        servers = rc.scan_servers()
        observed = environments.observe_live(rc_servers=servers)
        return servers, environments.current_envs(observed), environments.orphan_envs(observed)

    def fetch_pending(self, snapshot: WorldSnapshot | None = None) -> None:
        """Worker-thread data fetch. Only sets pending fields — no widgets."""
        if snapshot is not None:
            self.set_pending(snapshot.rc_projects)
            self._pending_servers = snapshot.rc_servers
            self._pending_current = environments.current_envs(snapshot.observed_envs)
            self._pending_orphans = environments.orphan_envs(snapshot.observed_envs)
        else:
            self.set_pending(rc.scan())
            servers, current, orphans = self._scan_extras()
            self._pending_servers = servers
            self._pending_current = current
            self._pending_orphans = orphans

    def set_pending(self, projects: list[RCProject]) -> None:
        self._pending = projects

    def apply_data(self) -> None:
        if self._pending is not None:
            self._projects = self._pending
            self._pending = None
            self._loaded = True
            if self._pending_servers is not None:
                self._servers = self._pending_servers
                self._pending_servers = None
            if self._pending_current is not None:
                self._current = self._pending_current
                self._pending_current = None
            if self._pending_orphans is not None:
                self._orphans = self._pending_orphans
                self._pending_orphans = None
            if not self._help:
                self._rebuild()

    def _rebuild(self) -> None:
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        # Projects first, so default focus lands on an actionable row.
        for p in self._projects:
            self.walker.append(RCRow(p))
        if self._servers:
            self.walker.append(_DividerRow("── RC 服务（外部只读）──"))
            for s in self._servers:
                self.walker.append(ServerRow(s))
        if self._current or self._orphans:
            self.walker.append(_DividerRow(f"── 环境台账（{_MANUAL_DELETE}）──"))
            for e in self._current:
                self.walker.append(EnvRow(e))
            for e in self._orphans:
                self.walker.append(EnvRow(e))
        if self.walker and focus_pos is not None:
            self.walker.set_focus(min(focus_pos, len(self.walker) - 1))

        running = sum(1 for p in self._projects if p.status == "running")
        auto = sum(1 for p in self._projects if p.auto_start)
        rc_off = sum(1 for p in self._projects if p.rc_at_startup is False)
        rc_text = f" · 自动远控关 {rc_off}" if rc_off else ""
        srv_text = f" · 服务 {len(self._servers)}" if self._servers else ""
        env_text = f" · 孤儿环境 {len(self._orphans)}" if self._orphans else ""
        self.status.original_widget.set_text(
            f" 共 {len(self._projects)} 项目 · 运行 {running} · 开机自启 {auto}"
            f"{rc_text}{srv_text}{env_text}"
        )

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
            self._show_help()

    def _show_help(self) -> None:
        self._help = True
        lines = [
            "远程控制操作（仅对「项目」行生效）:",
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
            "RC 服务 / 环境台账（只读）:",
            "  外部服务只展示，不接管、不重启。",
            f"  孤儿环境无法本地注销：{_MANUAL_DELETE}（claude.ai/code）。",
            "",
            "导航:",
            "  Tab    切换标签页",
            "  q      退出",
        ]
        self.walker.clear()
        for line in lines:
            self.walker.append(urwid.AttrMap(urwid.Text(line), "dead"))
        self.status.original_widget.set_text(" 按任意键返回")
        self._update_footer()
