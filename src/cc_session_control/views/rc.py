"""RC view — the 项目 tab (trusted projects + their Remote Control surface).

Shows two things:
  1. managed projects (RCProject) with typed `remoteControlAtStartup` evidence
     and `remoteControlSpawnMode`, plus Enter (start a NEW claude session in the
     project dir inside tmux, then enter it — the tmux-first launcher, ADR-0001),
     `o` (start the project RC server, the demoted secondary), and the
     stop/autostart keys;
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

from ..actions import tui_actions
from ..actions.session_ops import TmuxNewIntent
from ..data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from ..data.rc_enabled import EnabledListResult
from ..models import InventoryIssue, RCProject, RCServer, TrustDecision
from ._base import ListTabView
from ._colspec import ColSpec, header_columns, row_columns
from ._confirm import confirm_stop
from ._keytable import HelpLayout, Key, footer_hints, help_lines
from ._rows import SelectableRow, TextRow

if TYPE_CHECKING:
    from ..app import App
    from ..data.refresh import RefreshBatch

_STATUS_MAP = {
    "running": "● 运行中",
    "dead": "✖ 已退出",
    "stopped": "○ 已停止",
    "unknown": "？ 未知",
}
# Row attr per server/project status — dead (crashed pane) is a semantic error
# state and gets its own red entry (shape ✖ + word 已退出 + color: 3 channels).
_STATUS_ATTR = {
    "running": "alive",
    "dead": "status_err",
    "stopped": "dead",
    "unknown": "status_err",
}
_RC_FOCUS = {
    "alive": "selected",
    "status_err": "selected",
    "dead": "selected",
    None: "selected",
}
_RC_TRISTATE = {True: "开", False: "关", None: "未设置"}
# `c` cycles the per-project remoteControlAtStartup tri-state in full so the user
# can return to an explicit True (the old 2-cycle could never set True again).
_NEXT_TRISTATE = {None: True, True: False, False: None}

# One spec drives the tab header + project rows (_colspec.py).
_PROJECT_COLS: list[ColSpec] = [
    (10, "left", "状态"),
    (8, "left", "开机自启"),
    (8, "left", "自动远控"),
    (10, "left", "启动模式"),
    (("weight", 2), "left", "项目"),
    (("weight", 3), "left", "目录"),
]


class RCRow(SelectableRow):
    def __init__(self, project: RCProject) -> None:
        self.project = project
        # Focus identity for the shared rebuild — activity ordering may move
        # this row between refreshes; the cursor follows the path key.
        self.row_key = project.directory
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
        rc_at = (
            _RC_TRISTATE[project.rc_at_startup]
            if project.rc_at_startup_setting.available
            else "读取失败"
        )
        spawn = project.spawn_mode or "—"
        name = (
            project.name
            if project.in_list or project.status == "running"
            else f"({project.name})"
        )

        cols = row_columns(
            _PROJECT_COLS,
            [
                status_text,
                auto,
                rc_at,
                spawn,
                name,
                directory,
            ],
        )
        mapped = urwid.AttrMap(cols, attr, focus_map=_RC_FOCUS)
        super().__init__(mapped)


class _DividerRow(urwid.WidgetWrap):
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
        status_text = _STATUS_MAP.get(server.status, server.status)
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
        Key(
            ("enter",),
            "Enter 新建会话",
            "_key_tmux_new",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  Enter  在项目目录新建 tmux claude 会话并直接进入（离开 csctl；",
                "         tmux-first 主入口，会话默认获得断线保护）",
            ),
        ),
        Key(
            ("o",),
            "o 启动远控",
            "_key_start",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  o      启动选中项目的远程控制服务（手机/网页控制面，次要入口）",
            ),
        ),
        Key(
            ("s",),
            "s 停止",
            "_key_stop",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=("  s      停止选中项目的远程控制服务（需确认）",),
        ),
        Key(
            ("a",),
            "a 开机自启",
            "_key_autostart",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=("  a      切换「开机自启」：A 键一键启动时是否带上本项目",),
        ),
        Key(
            ("c",),
            "c 自动远控",
            "_key_rc_toggle",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  c      切换「自动远控」：claude 启动时自动开远程控制，手机即可接管",
            ),
        ),
        Key(
            ("A",),
            "A 全部启动",
            "_key_start_all",
            needs_selection=False,
            section="批量操作:",
            help_lines=("  A      启动所有「开机自启」项目",),
        ),
        Key(
            ("S",),
            "S 全部停止",
            "_key_stop_all",
            needs_selection=False,
            section="批量操作:",
            help_lines=("  S      停止全部远程控制服务（需确认）",),
        ),
        Key(
            ("r",),
            None,
            "_key_refresh",
            needs_selection=False,
            section="批量操作:",
            help_lines=("  r      重新扫描刷新",),
        ),
        Key(("?",), "? 详细说明", "_show_help", needs_selection=False),
    )

    HELP_LAYOUT = HelpLayout(
        sections=("项目操作（仅对「项目」行生效）:", "批量操作:"),
        suffix=(
            "目录缺失（✖ 缺失）:",
            "  项目目录已删除，但自启列表仍引用它（或其远控服务还在跑）；用 a 键移出自启列表。",
            "  只剩 claude 信任记录（~/.claude.json）引用的已删项目不再显示；信任记录需手动清理。",
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
        self._settings = ProjectSettingsResult(ProjectSettingsState.MISSING, {})
        self._enabled_list: EnabledListResult[tuple[str, ...]] | None = None
        self._environment_issue_count = 0
        self._inventory_issues: tuple[InventoryIssue, ...] = ()
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

    def apply_refresh(self, batch: RefreshBatch) -> None:
        """Apply one complete generation on the urwid main loop."""
        self._projects = list(batch.ordered_projects)
        self._settings = batch.snapshot.rc_project_settings
        self._enabled_list = batch.snapshot.rc_enabled_list
        self._servers = list(batch.snapshot.rc_servers)
        reconciliation = batch.snapshot.environment_reconciliation
        self._environment_issue_count = len(reconciliation.ledger.warnings) + int(
            reconciliation.ledger.failure is not None,
        )
        self._inventory_issues = reconciliation.inventory_issues
        self._loaded = True
        if not self._help:
            self._rebuild()

    def _build_rows(self) -> None:
        # Projects first, so default focus lands on an actionable row.
        for p in self._projects:
            self.walker.append(RCRow(p))
        if self._servers:
            self.walker.append(
                _DividerRow("── RC 服务（仅展示 · 托管见项目行 · 外部不可接管）──")
            )
            for s in self._servers:
                self.walker.append(ServerRow(s))
        if not self.walker:
            self.walker.append(urwid.AttrMap(urwid.Text(" 暂无远控项目"), "dead"))

    def _status_text(self) -> str:
        running = sum(1 for p in self._projects if p.status == "running")
        auto = sum(1 for p in self._projects if p.auto_start)
        rc_off = sum(1 for p in self._projects if p.rc_at_startup is False)
        rc_errors = sum(
            1 for p in self._projects if not p.rc_at_startup_setting.available
        )
        missing = sum(1 for p in self._projects if not p.dir_exists)
        rc_text = f" · 自动远控关 {rc_off}" if rc_off else ""
        rc_error_text = f" · 自动远控异常 {rc_errors}" if rc_errors else ""
        miss_text = f" · 目录缺失 {missing}" if missing else ""
        srv_text = f" · 服务 {len(self._servers)}" if self._servers else ""
        settings_text = (
            f" · 项目设置不可用（{self._settings.state.value}）"
            if not self._settings.available
            else ""
        )
        ledger_text = (
            f" · ⚠ 环境台账异常 {self._environment_issue_count}"
            if self._environment_issue_count
            else ""
        )
        enabled_issue = (
            self._enabled_list is not None and not self._enabled_list.success
        )
        inventory_count = len(self._inventory_issues) + int(enabled_issue)
        enabled_detail = ""
        if enabled_issue and self._enabled_list is not None:
            stage = (
                self._enabled_list.stage.value
                if self._enabled_list.stage is not None
                else "unknown"
            )
            committed = "；变更已提交，需刷新" if self._enabled_list.committed else ""
            enabled_detail = (
                f"（自启列表 {stage}：{self._enabled_list.detail}{committed}）"
            )
        inventory_text = (
            f" · ⚠ RC 清单不完整 {inventory_count}{enabled_detail}"
            if inventory_count
            else ""
        )
        return (
            f" 共 {len(self._projects)} 项目 · 运行 {running} · 开机自启 {auto}"
            f"{rc_text}{rc_error_text}{miss_text}{srv_text}{settings_text}"
            f"{ledger_text}{inventory_text}"
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
        if p.trust_decision is TrustDecision.UNAVAILABLE:
            self.app.notify("项目设置不可用 — 已拒绝启动")
            return
        if p.trust_decision is TrustDecision.UNTRUSTED:
            self.app.notify("未信任 — 先在该目录跑一次 claude")
            return
        if p.status == "unknown":
            self.app.notify("RC 清单不可用 — 已拒绝启动")
            return
        if p.status == "running":
            self.app.notify("已在运行")
            return
        path, name = p.directory, p.name
        self.app.submit_action(
            "project.start",
            lambda: tui_actions.start_project(path, name),
        )

    def _key_stop(self, p: RCProject) -> None:
        if p.status == "unknown":
            self.app.notify("RC 清单不可用 — 无法确认是否运行")
            return
        # gated=False: this stop kills a tmux window, not a pid — no R10 gate.
        confirm_stop(
            self.app,
            "远控服务",
            p.name,
            lambda: self._do_stop_one(p),
            alive=p.status == "running",
            gated=False,
        )

    def _key_autostart(self, p: RCProject) -> None:
        path, name = p.directory, p.name
        self.app.submit_action(
            "project.toggle-autostart",
            lambda: tui_actions.toggle_autostart(path, name),
        )

    def _key_rc_toggle(self, p: RCProject) -> None:
        if not p.dir_exists:
            # write_rc_at_startup would mkdir the deleted project back to life.
            self.app.notify("目录缺失 — 不写入配置")
            return
        setting = p.rc_at_startup_setting
        if not setting.available:
            source = f"：{setting.source}" if setting.source is not None else ""
            detail = f"：{setting.detail}" if setting.detail else ""
            self.app.notify(
                f"自动远控配置不可用（{setting.state.value}）"
                f"{source}{detail} — 不写入配置"
            )
            return
        # Full 3-cycle so explicit True is reachable again: None→True→False→None.
        new = _NEXT_TRISTATE[p.rc_at_startup]
        path, name = p.directory, p.name
        self.app.submit_action(
            "project.write-settings",
            lambda: tui_actions.write_auto_rc(path, name, new),
        )

    def _key_start_all(self) -> None:
        self.app.submit_action(
            "project.start-all",
            tui_actions.start_all_projects,
        )

    def _key_stop_all(self) -> None:
        if any(p.status == "unknown" for p in self._projects):
            self.app.notify("RC 清单不可用 — 无法确认是否运行")
            return
        if not any(p.status == "running" for p in self._projects):
            self.app.notify("本来就没在跑")
            return
        self.app.confirm("停止全部远控服务？", self._do_stop_all)

    def _do_stop_one(self, p: RCProject) -> None:
        """Stop-one body, run only after the y/n confirm accepts."""
        path, name = p.directory, p.name
        self.app.submit_action(
            "project.stop",
            lambda: tui_actions.stop_project(path, name),
        )

    def _do_stop_all(self) -> None:
        """Stop-all body, run only after the y/n confirm accepts."""
        self.app.submit_action(
            "project.stop-all",
            tui_actions.stop_all_projects,
        )

    def _show_help(self) -> None:
        """Help as a scrollable overlay, generated from KEY_TABLE."""
        self._help = True
        rows = [TextRow(line) for line in help_lines(self.KEY_TABLE, self.HELP_LAYOUT)]
        self._show_overlay("项目 / 远控帮助", rows)
        self._update_footer()
