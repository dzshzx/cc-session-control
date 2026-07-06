"""RC view — the 项目 tab (workspace projects + their Remote Control surface).

Shows two things:
  1. managed projects (RCProject) with the tri-state `remoteControlAtStartup`
     and `remoteControlSpawnMode`, plus the start/stop/autostart keys and `t`
     (start a NEW claude session in the project dir inside tmux, then enter it);
  2. project RC servers (RCServer) discovered via tmux ∪ /proc, badged
     managed/external — external servers are READ-ONLY (no takeover/restart key).

The bridge-environment ledger is deliberately NOT shown here: csctl cannot
deregister cloud environments, so the TUI doesn't list what it can't act on.
The ledger keeps recording in the background (snapshot upserts every cycle) and
stays queryable via `csctl env`.

Only project rows are actionable. Server rows are display-only, so no key
toggles RC on a running session or takes over an external server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..actions.session_ops import TmuxNewIntent
from ..data import rc
from ..data.rc import set_rc_at_startup
from ..models import RCProject, RCServer
from ._base import ListTabView
from ._colspec import header_columns, row_columns
from ._confirm import stop_message
from ._keytable import HelpLayout, Key, footer_hints, help_lines
from ._rows import TextRow

if TYPE_CHECKING:
    from ..data.snapshot import WorldSnapshot

    from ..app import App

_STATUS_MAP = {"running": "● 运行中", "dead": "✖ 已退出", "stopped": "○ 已停止"}
# Row attr per server/project status — dead (crashed pane) is a semantic error
# state and gets its own red entry (shape ✖ + word 已退出 + color: 3 channels).
_STATUS_ATTR = {"running": "alive", "dead": "status_err", "stopped": "dead"}
_RC_FOCUS = {"alive": "selected", "status_err": "selected", "dead": "selected", None: "selected"}
_RC_TRISTATE = {True: "开", False: "关", None: "未设置"}
# `c` cycles the per-project remoteControlAtStartup tri-state in full so the user
# can return to an explicit True (the old 2-cycle could never set True again).
_NEXT_TRISTATE = {None: True, True: False, False: None}

# One spec drives the tab header + project rows (_colspec.py).
_PROJECT_COLS = [
    (10, "left", "状态"),
    (8, "left", "开机自启"),
    (8, "left", "自动远控"),
    (10, "left", "启动模式"),
    (("weight", 2), "left", "项目"),
    (("weight", 3), "left", "目录"),
]


class RCRow(urwid.WidgetWrap):
    def __init__(self, project: RCProject) -> None:
        self.project = project
        status_text = _STATUS_MAP.get(project.status, project.status)
        attr = _STATUS_ATTR.get(project.status, "dead")
        directory = project.directory
        if not project.dir_exists:
            # Stale reference: claude.json / rc-enabled still list the project
            # but its workspace directory is gone. A running server (dir
            # deleted underneath it) keeps its status; otherwise 缺失 wins.
            directory += "（目录缺失）"
            if project.status != "running":
                status_text = "✖ 缺失"
                attr = "status_err"
        auto = "✓ 开" if project.auto_start else "✗ 关"
        rc_at = _RC_TRISTATE.get(project.rc_at_startup, "未设置")
        spawn = project.spawn_mode or "—"
        name = project.name if project.in_list or project.status == "running" else f"({project.name})"

        cols = row_columns(_PROJECT_COLS, [
            status_text, auto, rc_at, spawn, name, directory,
        ])
        mapped = urwid.AttrMap(cols, attr, focus_map=_RC_FOCUS)
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

    _COLS = [
        (10, "left", ""),
        (8, "left", ""),
        (8, "right", ""),
        (("weight", 2), "left", ""),
        (("weight", 3), "left", ""),
    ]

    def __init__(self, server: RCServer) -> None:
        self.server = server
        status_text = _STATUS_MAP.get(server.status, server.status)
        badge = "托管" if server.managed else "外部"
        pid = str(server.pid) if server.pid else "-"
        cols = row_columns(self._COLS, [
            status_text, badge, pid, server.name, server.cwd or "",
        ])
        attr = _STATUS_ATTR.get(server.status, "dead")
        mapped = urwid.AttrMap(cols, attr, focus_map=_RC_FOCUS)
        super().__init__(mapped)

    def selectable(self) -> bool:
        # P4: display-only — focus SKIPS it (like _DividerRow) so the user never
        # lands on a highlighted row whose keys are all silently inert.
        return False

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class RCView(ListTabView):
    # Single source for every list-mode key (views/_keytable.py): footer,
    # help, and dispatch are generated from this table. `r 刷新` stays in the
    # App-level FOOTER_PREFIX, so its entry is hint-less.
    KEY_TABLE = (
        Key(("t",), "t 新建会话", "_key_tmux_new",
            section="项目操作（仅对「项目」行生效）:", help_lines=(
                "  t      在项目目录新建 tmux claude 会话并直接进入（离开 csctl）",
            )),
        Key(("enter",), "Enter 启动远控", "_key_start",
            section="项目操作（仅对「项目」行生效）:", help_lines=(
                "  Enter  启动选中项目的远程控制服务",
            )),
        Key(("s",), "s 停止", "_key_stop",
            section="项目操作（仅对「项目」行生效）:", help_lines=(
                "  s      停止选中项目的远程控制服务（需确认）",
            )),
        Key(("a",), "a 开机自启", "_key_autostart",
            section="项目操作（仅对「项目」行生效）:", help_lines=(
                "  a      切换「开机自启」：A 键一键启动时是否带上本项目",
            )),
        Key(("c",), "c 自动远控", "_key_rc_toggle",
            section="项目操作（仅对「项目」行生效）:", help_lines=(
                "  c      切换「自动远控」：claude 启动时自动开远程控制，手机即可接管",
            )),
        Key(("A",), "A 全部启动", "_key_start_all", needs_selection=False,
            section="批量操作:", help_lines=(
                "  A      启动所有「开机自启」项目",
            )),
        Key(("S",), "S 全部停止", "_key_stop_all", needs_selection=False,
            section="批量操作:", help_lines=(
                "  S      停止全部远程控制服务（需确认）",
            )),
        Key(("r",), None, "_key_refresh", needs_selection=False,
            section="批量操作:", help_lines=(
                "  r      重新扫描刷新",
            )),
        Key(("?",), "? 详细说明", "_show_help", needs_selection=False),
    )

    HELP_LAYOUT = HelpLayout(
        sections=("项目操作（仅对「项目」行生效）:", "批量操作:"),
        suffix=(
            "目录缺失（✖ 缺失）:",
            "  项目目录已删除，但 claude 的信任记录（~/.claude.json）或自启列表仍引用它。",
            "  csctl 不改写 claude 的文件；用 a 键可将其移出自启列表，信任记录需手动清理。",
            "",
            "RC 服务（只读）:",
            "  外部服务只展示，不接管、不重启。",
            "  云端环境台账不在 TUI 展示（csctl 无法注销云端环境）；查询用 csctl env。",
            "",
            "导航:",
            "  Tab    切换标签页",
            "  q      退出",
        ),
    )

    def __init__(self, app: App) -> None:
        super().__init__(app, header_columns(_PROJECT_COLS))
        self._projects: list[RCProject] = []
        self._servers: list[RCServer] = []
        self._pending: list[RCProject] | None = None
        self._pending_servers: list[RCServer] | None = None
        self._help = False

    def keyhints(self) -> str:
        if self._overlay_active():
            # "其余" is honest: the prefix's Tab/q stay global (Tab switches
            # tabs, q QUITS — neither returns to the list).
            return "其余任意键返回"
        # Every list-mode key gets a brief hint, straight from KEY_TABLE; the
        # footer Text wraps on narrow terminals (vertical for width).
        return footer_hints(self.KEY_TABLE)

    def _overlay_active(self) -> bool:
        return self._help

    def load(self) -> None:
        self._projects = rc.scan()
        self._servers = rc.scan_servers()
        self._loaded = True
        self._rebuild()

    def fetch_pending(self, snapshot: WorldSnapshot | None = None) -> None:
        """Worker-thread data fetch. Only sets pending fields — no widgets."""
        if snapshot is not None:
            self.set_pending(snapshot.rc_projects)
            self._pending_servers = snapshot.rc_servers
        else:
            self.set_pending(rc.scan())
            self._pending_servers = rc.scan_servers()

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
            if not self._help:
                self._rebuild()

    def _build_rows(self) -> None:
        # Projects first, so default focus lands on an actionable row.
        for p in self._projects:
            self.walker.append(RCRow(p))
        if self._servers:
            self.walker.append(_DividerRow("── RC 服务（仅展示 · 托管见项目行 · 外部不可接管）──"))
            for s in self._servers:
                self.walker.append(ServerRow(s))
        if not self.walker:
            self.walker.append(urwid.AttrMap(urwid.Text(" 暂无远控项目"), "dead"))

    def _status_text(self) -> str:
        running = sum(1 for p in self._projects if p.status == "running")
        auto = sum(1 for p in self._projects if p.auto_start)
        rc_off = sum(1 for p in self._projects if p.rc_at_startup is False)
        missing = sum(1 for p in self._projects if not p.dir_exists)
        rc_text = f" · 自动远控关 {rc_off}" if rc_off else ""
        miss_text = f" · 目录缺失 {missing}" if missing else ""
        srv_text = f" · 服务 {len(self._servers)}" if self._servers else ""
        return (
            f" 共 {len(self._projects)} 项目 · 运行 {running} · 开机自启 {auto}"
            f"{rc_text}{miss_text}{srv_text}"
        )

    def _close_overlay_mode(self) -> None:
        self._help = False

    def _selected(self) -> RCProject | None:
        widget = self._focused_widget()
        if isinstance(widget, RCRow):
            return widget.project
        return None

    # --- key handlers (bound by name in KEY_TABLE; dispatch lives in the base) ---

    def _key_tmux_new(self, p: RCProject) -> None:
        if not p.dir_exists:
            self.app.notify("目录缺失 — 无法新建会话")
            return
        # New claude session in the project dir, inside tmux, entered
        # immediately — nothing is killed, so no confirm / R10 / trust gate
        # (claude's own trust dialog shows interactively in the window).
        self.app.exit_with(TmuxNewIntent(p.directory))

    def _key_start(self, p: RCProject) -> None:
        if not p.dir_exists:
            self.app.notify("目录缺失 — 无法启动（可用 a 键移出自启列表）")
            return
        if not p.trusted:
            self.app.notify("未信任 — 先在该目录跑一次 claude")
            return
        if p.status == "running":
            self.app.notify("已在运行")
            return
        ok = rc.start_one(p.name)
        self.app.notify(f"已启动 ws/{p.name}" if ok else "启动失败")
        self.app.trigger_async_refresh()

    def _key_stop(self, p: RCProject) -> None:
        if p.status != "running":
            self.app.notify("未在运行")
            return
        self.app.confirm(
            stop_message("停止远控服务", p.name),
            lambda: self._do_stop_one(p.name),
        )

    def _key_autostart(self, p: RCProject) -> None:
        new = rc.toggle_autostart(p.name)
        self.app.notify(f"{p.name} 开机自启: {'开' if new else '关'}")
        self.app.trigger_async_refresh()

    def _key_rc_toggle(self, p: RCProject) -> None:
        if not p.dir_exists:
            # set_rc_at_startup would mkdir the deleted project back to life.
            self.app.notify("目录缺失 — 不写入配置")
            return
        # Full 3-cycle so explicit True is reachable again: None→True→False→None.
        new = _NEXT_TRISTATE[p.rc_at_startup]
        set_rc_at_startup(p.directory, new)
        self.app.notify(f"{p.name} 自动远控: {_RC_TRISTATE[new]}")
        self.app.trigger_async_refresh()

    def _key_start_all(self) -> None:
        count = rc.start_all_listed()
        self.app.notify(f"已启动 {count} 个项目")
        self.app.trigger_async_refresh()

    def _key_stop_all(self) -> None:
        if not any(p.status == "running" for p in self._projects):
            self.app.notify("本来就没在跑")
            return
        self.app.confirm("停止全部远控服务？", self._do_stop_all)

    def _do_stop_one(self, name: str) -> None:
        """Stop-one body, run only after the y/n confirm accepts."""
        ok = rc.stop_one(name)
        self.app.notify(f"已停止 {name}" if ok else "未在运行")
        self.app.trigger_async_refresh()

    def _do_stop_all(self) -> None:
        """Stop-all body, run only after the y/n confirm accepts."""
        ok = rc.stop_all()
        self.app.notify("已停止全部" if ok else "本来就没在跑")
        self.app.trigger_async_refresh()

    def _show_help(self) -> None:
        """Help as a scrollable overlay, generated from KEY_TABLE."""
        self._help = True
        rows = [TextRow(line) for line in help_lines(self.KEY_TABLE, self.HELP_LAYOUT)]
        self._show_overlay("项目 / 远控帮助", rows)
        self._update_footer()
