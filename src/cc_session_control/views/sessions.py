"""Sessions view — urwid ListBox with keyboard actions and cleanup submenu."""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..actions.session_ops import (
    AttachIntent,
    ResumeIntent,
    TmuxResumeIntent,
    attach_target,
    relaunch_in_tmux,
    resume_cmd,
    terminate_session,
    to_clipboard,
)
from ..data import liveness, proc, registry
from ..data.cleanup import cleanup_classified, remove_session
from ..data.sessions import scan
from ..models import AgentJob, Session, SessionProc
from ._base import ListTabView
from ._confirm import DEGRADED as _DEGRADED
from ._confirm import confirm_takeover, stop_message
from ._keytable import HelpLayout, Key, footer_hints, help_lines
from ._rows import TextRow
from ._session_row import (
    _SESSION_HEADER,
    SessionRow,
    _hidden_marker,
)
from ._sessions_cleanup import CleanupMixin

if TYPE_CHECKING:
    from ..data.snapshot import WorldSnapshot

    from ..app import App


class SessionsView(CleanupMixin, ListTabView):
    # mode: "list" | "filter" | "cleanup" | "preview" | "help"

    OVERLAY_WIDTH = 70

    # Single source for every list-mode key: footer hints, help overlay, and
    # dispatch are all generated from this table (views/_keytable.py). Full key
    # table in the footer is a user preference (2026-07-05); `r 刷新` stays in
    # the App-level FOOTER_PREFIX, so its entry is hint-less here.
    KEY_TABLE = (
        Key(("enter",), "Enter 接回", "_key_resume", section="会话操作:", help_lines=(
            "  Enter  接回选中的会话（在当前终端恢复；接运行中的会话会先确认接管）",
        )),
        Key(("t",), "t tmux接回", "_key_tmux", section="会话操作:", help_lines=(
            "  t      tmux 接回（会话恢复进 tmux 窗口并接入前台——终端断线会话不死，",
            "         重连后 tmux attach 可捡回；已在 tmux 中的会话直接接入不重启；",
            "         接运行中的裸终端会话会先确认接管）",
        )),
        Key(("f",), "f 分叉", "_key_fork", section="会话操作:", help_lines=(
            "  f      分叉会话（创建副本后接回，不影响原会话）",
        )),
        Key(("s",), "s 停止", "_key_stop", section="会话操作:", help_lines=(
            "  s      停止运行中的会话（发送 SIGTERM，需二次确认）",
        )),
        Key(("R",), "R 转后台", "_key_relaunch", section="会话操作:", help_lines=(
            "  R      转入 tmux 后台并开启远控（脱离终端，手机/网页可接管；",
            "         接运行中的会话会先确认接管）",
        )),
        Key(("d",), "d 删除", "_key_delete", section="会话操作:", help_lines=(
            "  d      删除已结束的会话记录",
        )),
        Key(("y",), "y 复制命令", "_key_yank", section="会话操作:", help_lines=(
            "  y      复制接回命令到剪贴板",
        )),
        Key(("h",), "h 桥接显隐", "_key_toggle_hidden", needs_selection=False,
            section="会话操作:", help_lines=(
                "  h      显示/隐藏桥接、SDK 会话",
            )),
        Key(("c",), "c 清理", "_enter_cleanup", needs_selection=False,
            section="清理与过滤:", help_lines=(
                "  c      打开清理子菜单",
            )),
        Key(("/",), "/ 过滤", "_enter_filter", needs_selection=False,
            section="清理与过滤:", help_lines=(
                "  /      按关键词过滤会话列表",
            )),
        Key(("r",), None, "_key_refresh", needs_selection=False,
            section="清理与过滤:", help_lines=(
                "  r      刷新",
            )),
        Key(("?",), "? 详细说明", "_show_help", needs_selection=False),
    )

    HELP_LAYOUT = HelpLayout(
        prefix=(
            "状态列: ● 忙 = 正在生成/执行工具 · ● 闲 = 等待输入 · ○ 停 = 无进程",
            "        ▸ = 当前会话（启动 csctl 的会话，受保护） · 📱 = 已开远控",
            "",
        ),
        sections=("会话操作:", "清理与过滤:"),
        suffix=(
            "导航:",
            "  Tab    切换标签页",
            "  q      退出",
        ),
    )

    def __init__(self, app: App) -> None:
        super().__init__(app, _SESSION_HEADER)
        self._sessions: list[Session] = []
        self._all_sessions: list[Session] = []
        self._pending: list[Session] | None = None
        self._mode = "list"
        self._filter_text = ""
        self._cleanup_stats: dict[str, int] = {}
        self._classified: dict[str, int] = {}
        self._preview_action: str | None = None
        self._preview_sessions: list[Session] = []
        self._show_hidden = True
        # Shared-snapshot liveness inputs for the pid-keyed zombie sweep + the
        # classified counts (R11/D8 — projected, never re-scanned per view).
        self._session_procs: list[SessionProc] = []
        self._cur: set[int] = set()
        self._pending_procs: list[SessionProc] | None = None
        self._pending_cur: set[int] | None = None
        self._pending_classified: dict[str, int] | None = None
        self._cleanup_walker = urwid.SimpleFocusListWalker([])

    def keyhints(self) -> str:
        if self._overlay_active():
            # "其余" is honest: the prefix's Tab/q stay global (Tab switches
            # tabs, q QUITS — neither returns to the list).
            return "其余任意键返回"
        if self._mode == "cleanup":
            return "Enter 预览待清理项 · Esc 返回会话列表"
        if self._mode == "preview":
            return "Enter 确认清理 · Esc 取消"
        # Every list-mode key gets a brief hint, straight from KEY_TABLE; the
        # footer Text wraps (urwid wrap='space'), trading rows for width.
        return footer_hints(self.KEY_TABLE)

    def load(self) -> None:
        sessions = scan()
        procs, cur, jobs, agents = self._self_fetch_liveness()
        self._all_sessions = sessions
        self._session_procs = procs
        self._cur = cur
        self._classified = self._classify(sessions, procs, cur, jobs, agents)
        self._cleanup_stats = self._derive_stats(sessions, self._classified)
        self._loaded = True
        self._apply_filter()
        self._rebuild()

    def _self_fetch_liveness(
        self,
    ) -> tuple[list[SessionProc], set[int], list[AgentJob], dict[str, int | None]]:
        """No-snapshot liveness inputs (back-compat / tests). Swallows errors.

        Mirrors what `build_world_snapshot` computes so the submenu counts + the
        zombie sweep work even without a shared snapshot. Liveness injection goes
        through the one `liveness.live_session_procs` seam, same as the snapshot.
        """
        procs = liveness.live_session_procs()
        try:
            jobs = registry.read_agent_jobs()
        except Exception:
            jobs = []
        try:
            agents = liveness.alive_map()
        except Exception:
            agents = {}
        return procs, proc.ancestor_pids(), jobs, agents

    def _classify(
        self,
        sessions: list[Session],
        procs: list[SessionProc],
        cur: set[int],
        jobs: list[AgentJob],
        agents: dict[str, int | None],
    ) -> dict[str, int]:
        try:
            return cleanup_classified(sessions, procs, cur, jobs, agents)
        except Exception:
            return {}

    def _derive_stats(self, sessions: list[Session], classified: dict[str, int]) -> dict[str, int]:
        """The legacy 4-key status-bar shape, derived from the classified counts."""
        return {
            "total": len(sessions),
            "empty": classified.get("empty", 0),
            "short": classified.get("short", 0),
            "orphans": classified.get("orphan_dirs", 0),
        }

    def fetch_pending(self, snapshot: WorldSnapshot | None = None) -> None:
        """Worker-thread data fetch. Only sets pending fields — no widgets.

        Projects the shared `snapshot` when given (R11/D8 — no per-view re-scan);
        falls back to a self-contained scan when called with no snapshot
        (back-compat / tests). The liveness inputs (`session_procs`/`cur` +
        `agent_jobs`/`agents_map`) feed the pid-keyed zombie sweep and the
        classified counts — taken straight from the snapshot, never re-scanned.
        """
        if snapshot is not None:
            sessions = snapshot.sessions
            procs, cur = snapshot.session_procs, snapshot.cur
            jobs, agents = snapshot.agent_jobs, snapshot.agents_map
        else:
            sessions = scan()
            procs, cur, jobs, agents = self._self_fetch_liveness()
        classified = self._classify(sessions, procs, cur, jobs, agents)
        self.set_pending(sessions)
        self._pending_procs = procs
        self._pending_cur = cur
        self._pending_classified = classified
        self.set_pending_stats(self._derive_stats(sessions, classified))

    def set_pending(self, sessions: list[Session]) -> None:
        self._pending = sessions

    def set_pending_stats(self, stats: dict[str, int]) -> None:
        self._cleanup_stats = stats

    def apply_data(self) -> None:
        if self._pending is not None:
            self._all_sessions = self._pending
            self._pending = None
            if self._pending_procs is not None:
                self._session_procs = self._pending_procs
                self._pending_procs = None
            if self._pending_cur is not None:
                self._cur = self._pending_cur
                self._pending_cur = None
            if self._pending_classified is not None:
                self._classified = self._pending_classified
                self._pending_classified = None
            self._loaded = True
            if self._mode == "list" or self._mode == "filter":
                self._apply_filter()
                self._rebuild()
            elif self._mode == "cleanup":
                self._rebuild_cleanup()

    def _build_rows(self) -> None:
        for s in self._sessions:
            self.walker.append(SessionRow(s))
        if not self._sessions:
            empty = "无匹配 · 按 / 改过滤 · Esc 清空" if self._filter_text else "暂无会话"
            self.walker.append(urwid.AttrMap(urwid.Text(f" {empty}"), "dead"))

    def _status_text(self) -> str:
        alive_n = sum(1 for s in self._all_sessions if s.alive)
        flt = f" · 过滤「{self._filter_text}」" if self._filter_text else ""
        empty = self._cleanup_stats.get("empty", 0)
        short = self._cleanup_stats.get("short", 0)
        orphans = self._cleanup_stats.get("orphans", 0)
        cleanup_text = ""
        hidden_n = sum(1 for s in self._all_sessions if s.bridge_or_sdk)
        hidden_text = ""
        if hidden_n:
            hidden_text = f" · 桥接/SDK {hidden_n}" if self._show_hidden else f" · 桥接/SDK已隐藏 {hidden_n}"
        if empty or short or orphans:
            parts = []
            if empty:
                parts.append(f"空壳 {empty}")
            if short:
                parts.append(f"短 {short}")
            if orphans:
                parts.append(f"孤儿 {orphans}")
            cleanup_text = f" · {' · '.join(parts)}"
        return (
            f" 共 {len(self._all_sessions)} 条会话 · 运行 {alive_n} · 显示 {len(self._sessions)}{flt}{hidden_text}{cleanup_text}"
        )

    def _close_overlay_mode(self) -> None:
        self._mode = "list"

    def _selected(self) -> Session | None:
        widget = self._focused_widget()
        if isinstance(widget, SessionRow):
            return widget.session
        return None

    def _apply_filter(self) -> None:
        # D9: the hide filter unions the transcript `hidden` tags with the
        # registry `source == "sdk"` signal (Session.bridge_or_sdk), so the
        # badge and the `h` toggle never disagree.
        visible = [
            s for s in self._all_sessions
            if self._show_hidden or not s.bridge_or_sdk
        ]
        if not self._filter_text:
            self._sessions = visible
        else:
            k = self._filter_text.lower()
            self._sessions = [
                s for s in visible
                if k in (
                    s.label + " " + s.cwd + " " + s.sid + " "
                    + _hidden_marker(s) + " " + " ".join(sorted(s.hidden))
                ).lower()
            ]

    def _enter_filter(self) -> None:
        self._mode = "filter"
        self._filter_edit = urwid.Edit("过滤: ")
        self.app.own_footer(urwid.AttrMap(self._filter_edit, "notify"))

    def deactivate(self) -> None:
        """TabView hook: called on tab switch-away. Commits + closes a transient
        filter — its Edit lives in the App footer and turns invisible once the
        next tab's hints replace it; without this, keys after switching back
        would still edit the hidden filter (mode leak)."""
        if self._mode == "filter":
            self._exit_filter()

    def _exit_filter(self, cancel: bool = False) -> None:
        self._mode = "list"
        if cancel:
            self._filter_text = ""
        else:
            self._filter_text = self._filter_edit.get_edit_text()
        self._apply_filter()
        self._rebuild()
        self.app.release_footer()

    def _do_terminate(self, s: Session) -> None:
        """Stop body, run only after the y/n confirm accepts."""
        ok = terminate_session(s)
        self.app.notify("已停止" if ok else "停止失败")
        self.app.trigger_async_refresh()

    def _do_relaunch(self, s: Session) -> None:
        """Relaunch-into-tmux body (after confirm when it takes over a live one)."""
        ok = relaunch_in_tmux(s)
        self.app.notify(
            "已转入后台 + 远控（手机/网页可接管）" if ok else "转入后台失败"
        )
        self.app.trigger_async_refresh()

    def _resume_or_confirm(self, s: Session, fork: bool) -> None:
        """Resume now, or confirm first when it would take over a live session."""
        confirm_takeover(
            self.app, s, "接回会话",
            lambda: self.app.exit_with(ResumeIntent(s, fork)), fork=fork,
        )

    def _tmux_resume_or_confirm(self, s: Session) -> None:
        """`t` key: attach when already tmux-hosted, else resume inside tmux.

        A live tmux-hosted session is entered in place — no kill, no confirm, no
        R10 gate (nothing destructive). Otherwise mirrors `R`.
        """
        target = attach_target(s)
        if target:
            self.app.exit_with(AttachIntent(target))
            return
        confirm_takeover(
            self.app, s, "tmux 接回", lambda: self.app.exit_with(TmuxResumeIntent(s))
        )

    # --- Key dispatch ---

    def _overlay_active(self) -> bool:
        return self._mode == "help"

    def handle_key(self, key: str) -> None:
        """Extra modes first (filter/preview/cleanup are Sessions-only); the
        help overlay + list dispatch fall through to the base handle_key."""
        if self._mode == "filter":
            if key == "enter":
                self._exit_filter()
            elif key == "esc":
                self._exit_filter(cancel=True)
            else:
                self._filter_edit.keypress((80,), key)
            return

        if self._mode == "preview":
            if key == "enter":
                self._confirm_cleanup()
            elif key == "esc":
                self._enter_cleanup()
            elif key == "r":
                # Footer prefix promises `r 刷新` on every tab/mode — honor it
                # (the preview list itself stays as computed at entry).
                self.app.refresh_with_notice()
            return

        if self._mode == "cleanup":
            if key == "enter":
                action = self._selected_action()
                if action:
                    self._enter_preview(action)
            elif key == "esc":
                self._exit_cleanup()
            elif key == "r":
                self.app.refresh_with_notice()
            return

        # Help overlay + normal list mode: the base handle_key.
        super().handle_key(key)

    # --- key handlers (bound by name in KEY_TABLE) ---

    def _key_resume(self, s: Session) -> None:
        if s.current:
            self.app.notify("不能接回当前会话")
            return
        self._resume_or_confirm(s, fork=False)

    def _key_tmux(self, s: Session) -> None:
        if s.current:
            self.app.notify("不能 tmux 接回当前会话")
            return
        self._tmux_resume_or_confirm(s)

    def _key_fork(self, s: Session) -> None:
        if s.current:
            self.app.notify("不能分叉当前会话")
            return
        self.app.exit_with(ResumeIntent(s, fork=True))

    def _key_stop(self, s: Session) -> None:
        # Degrade gate FIRST (R2): off /proc every pid looks dead, so a stop
        # could hit csctl's own session — refuse honestly before confirming.
        if not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return
        if not s.alive:
            self.app.notify("会话未在运行")
            return
        if s.current:
            self.app.notify("不能停止当前会话")
            return
        self.app.confirm(
            stop_message("停止会话", s.label),
            lambda: self._do_terminate(s),
        )

    def _key_relaunch(self, s: Session) -> None:
        if s.current:
            self.app.notify("不能转入后台当前会话")
            return
        confirm_takeover(self.app, s, "转入后台", lambda: self._do_relaunch(s))

    def _key_delete(self, s: Session) -> None:
        if s.alive:
            self.app.notify("运行中的会话不删，先停止")
            return
        if not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return
        # L4: honour remove_session's bool — only claim success when it truly
        # removed something; a False here means there was nothing to delete.
        if remove_session(s):
            self.app.notify("已删除")
        else:
            self.app.notify("无可删除内容")
        self.app.trigger_async_refresh()

    def _key_yank(self, s: Session) -> None:
        cmd = resume_cmd(s)
        ok = to_clipboard(cmd)
        self.app.notify("已复制" if ok else f"复制失败: {cmd}")

    def _key_toggle_hidden(self) -> None:
        self._show_hidden = not self._show_hidden
        self._apply_filter()
        self._rebuild()
        self._update_footer()

    def _show_help(self) -> None:
        rows = [TextRow(line) for line in help_lines(self.KEY_TABLE, self.HELP_LAYOUT)]
        self._mode = "help"
        self._show_overlay("快捷键帮助", rows)
        self._update_footer()
