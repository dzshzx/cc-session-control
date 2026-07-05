"""Sessions view — urwid ListBox with keyboard actions and cleanup submenu."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import urwid

from ..actions.session_ops import (
    attach_target,
    relaunch_in_tmux,
    resume_cmd,
    terminate_session,
    to_clipboard,
    would_take_over,
)
from ..data import liveness, proc, registry
from ..data.cleanup import cleanup_classified, remove_session
from ..data.sessions import scan
from ..models import AgentJob, Session, SessionProc
from ._session_row import (
    _SESSION_HEADER,
    SessionRow,
    _hidden_marker,
    _PreviewRow,
)
from ._sessions_cleanup import _DEGRADED, CleanupMixin

if TYPE_CHECKING:
    from ..data.snapshot import WorldSnapshot

    from ..app import App


class SessionsView(CleanupMixin):
    # mode: "list" | "filter" | "cleanup" | "preview" | "help"
    def __init__(self, app: App) -> None:
        self.app = app
        self._sessions: list[Session] = []
        self._all_sessions: list[Session] = []
        self._pending: list[Session] | None = None
        self._loaded = False
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

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        col_header = urwid.AttrMap(_SESSION_HEADER, "col_header")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        self._list_body = urwid.AttrMap(self.listbox, {None: "body"})
        self._body = urwid.WidgetPlaceholder(self._list_body)
        self.widget = urwid.Frame(self._body, header=col_header, footer=self.status)
        self._cleanup_walker = urwid.SimpleFocusListWalker([])

    def keyhints(self) -> str:
        if self._mode == "help":
            return "按任意键返回"
        if self._mode == "cleanup":
            return "Enter 预览待清理项 · Esc 返回会话列表"
        if self._mode == "preview":
            return "Enter 确认清理 · Esc 取消"
        # Full key table (user preference 2026-07-05): every list-mode key gets a
        # brief hint; `?` holds the detailed semantics. The footer Text wraps
        # (urwid wrap='space'), trading vertical rows for width on narrow
        # terminals. `r 刷新` stays in the App-level footer prefix, not here.
        return (
            "Enter 接回 · t tmux接回 · f 分叉 · s 停止 · R 转后台 · d 删除 · "
            "y 复制命令 · h 桥接显隐 · c 清理 · / 过滤 · ? 详细说明"
        )

    def _update_footer(self) -> None:
        if self.app.views[self.app._active] is not self:
            return
        self.app.set_hints(self.keyhints())

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
        zombie sweep work even without a shared snapshot. `proc_alive` is injected
        here exactly as the snapshot path does it.
        """
        try:
            procs = [
                replace(sp, proc_alive=proc.pid_alive(sp.pid, sp.proc_start))
                for sp in registry.read_session_procs()
            ]
        except Exception:
            procs = []
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

    def _rebuild(self) -> None:
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        for s in self._sessions:
            self.walker.append(SessionRow(s))
        if not self._sessions:
            empty = "无匹配 · 按 / 改过滤 · Esc 清空" if self._filter_text else "暂无会话"
            self.walker.append(urwid.AttrMap(urwid.Text(f" {empty}"), "dead"))
        if self.walker and focus_pos is not None:
            self.walker.set_focus(min(focus_pos, len(self.walker) - 1))
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
        self.status.original_widget.set_text(
            f" 共 {len(self._all_sessions)} 条会话 · 运行 {alive_n} · 显示 {len(self._sessions)}{flt}{hidden_text}{cleanup_text}"
        )

    def _selected(self) -> Session | None:
        if not self.walker:
            return None
        widget = self.walker.get_focus()[0]
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
        self.app.frame.footer = urwid.AttrMap(self._filter_edit, "notify")

    def _exit_filter(self, cancel: bool = False) -> None:
        self._mode = "list"
        if cancel:
            self._filter_text = ""
        else:
            self._filter_text = self._filter_edit.get_edit_text()
        self._apply_filter()
        self._rebuild()
        self.app._restore_footer()

    def _show_overlay(self, title: str, rows: list, height: int | None = None) -> None:
        preview_walker = urwid.SimpleFocusListWalker(rows)
        preview_list = urwid.ListBox(preview_walker)
        header = urwid.AttrMap(urwid.Text(f" {title}", align="center"), "col_header")
        box = urwid.LineBox(urwid.Frame(preview_list, header=header))
        h = height or min(len(rows) + 4, 30)
        overlay = urwid.Overlay(
            box, self._list_body,
            align="center", width=("relative", 70),
            valign="middle", height=h,
        )
        self._body.original_widget = overlay

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
        """Resume now, or confirm first when it would take over a live session.

        Reads `would_take_over` (= should_kill, the single source) so the confirm
        gate never re-derives the takeover condition (CLAUDE.md invariant).
        """
        if would_take_over(s, fork):
            self.app.confirm(
                f"接回会话「{s.label[:30]}」？将先终止原进程。",
                lambda: self.app.exit_with_resume(s, fork),
            )
        else:
            self.app.exit_with_resume(s, fork)

    def _tmux_resume_or_confirm(self, s: Session) -> None:
        """`t` key: attach when already tmux-hosted, else resume inside tmux.

        A live tmux-hosted session is entered in place — no kill, no confirm, no
        R10 gate (nothing destructive). Otherwise mirrors `R`: `would_take_over`
        (the single should_kill source) decides the confirm + degrade gate.
        """
        target = attach_target(s)
        if target:
            self.app.exit_with_attach(target)
            return
        if would_take_over(s) and not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return
        if would_take_over(s):
            self.app.confirm(
                f"tmux 接回「{s.label[:30]}」？将先终止原进程。",
                lambda: self.app.exit_with_tmux_resume(s),
            )
        else:
            self.app.exit_with_tmux_resume(s)

    # --- Key dispatch ---

    def handle_key(self, key: str) -> None:
        if self._mode == "help":
            self._mode = "list"
            self._body.original_widget = self._list_body
            self._update_footer()
            return

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
            return

        if self._mode == "cleanup":
            if key == "enter":
                action = self._selected_action()
                if action:
                    self._enter_preview(action)
            elif key == "esc":
                self._exit_cleanup()
            elif key == "r":
                self.app.trigger_async_refresh()
                self.app.notify("刷新中…")
            return

        # Normal list mode
        s = self._selected()

        if key == "enter" and s:
            if s.current:
                self.app.notify("不能接回当前会话")
                return
            self._resume_or_confirm(s, fork=False)
        elif key == "t" and s:
            if s.current:
                self.app.notify("不能 tmux 接回当前会话")
                return
            self._tmux_resume_or_confirm(s)
        elif key == "f" and s:
            if s.current:
                self.app.notify("不能分叉当前会话")
                return
            self.app.exit_with_resume(s, fork=True)
        elif key == "s" and s:
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
                f"停止会话「{s.label[:30]}」？将终止其进程。",
                lambda: self._do_terminate(s),
            )
        elif key == "R" and s:
            if s.current:
                self.app.notify("不能转入后台当前会话")
                return
            # Degrade gate only for a takeover (live): relaunching a DEAD session
            # kills nothing and data refuses nothing, so it stays usable off
            # /proc (B3). `would_take_over` is the single should_kill source.
            if would_take_over(s) and not proc.current_determinable():
                self.app.notify(_DEGRADED)
                return
            if would_take_over(s):
                self.app.confirm(
                    f"转入后台「{s.label[:30]}」？将先终止原进程。",
                    lambda: self._do_relaunch(s),
                )
            else:
                self._do_relaunch(s)
        elif key == "d" and s:
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
        elif key == "y" and s:
            cmd = resume_cmd(s)
            ok = to_clipboard(cmd)
            self.app.notify("已复制" if ok else f"复制失败: {cmd}")
        elif key == "c":
            self._enter_cleanup()
        elif key == "h":
            self._show_hidden = not self._show_hidden
            self._apply_filter()
            self._rebuild()
            self._update_footer()
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
        elif key == "/":
            self._enter_filter()
        elif key == "?":
            self._show_help()

    def _show_help(self) -> None:
        lines = [
            "状态列: ● 忙 = 正在生成/执行工具 · ● 闲 = 等待输入 · ○ 停 = 无进程",
            "        ▸ = 当前会话（启动 csctl 的会话，受保护） · 📱 = 已开远控",
            "",
            "会话操作:",
            "  Enter  接回选中的会话（在当前终端恢复；接运行中的会话会先确认接管）",
            "  t      tmux 接回（会话恢复进 tmux 窗口并接入前台——终端断线会话不死，",
            "         重连后 tmux attach 可捡回；已在 tmux 中的会话直接接入不重启；",
            "         接运行中的裸终端会话会先确认接管）",
            "  f      分叉会话（创建副本后接回，不影响原会话）",
            "  s      停止运行中的会话（发送 SIGTERM，需二次确认）",
            "  R      转入 tmux 后台并开启远控（脱离终端，手机/网页可接管；",
            "         接运行中的会话会先确认接管）",
            "  d      删除已结束的会话记录",
            "  y      复制接回命令到剪贴板",
            "  h      显示/隐藏桥接、SDK 会话",
            "",
            "清理与过滤:",
            "  c      打开清理子菜单",
            "  /      按关键词过滤会话列表",
            "  r      刷新",
            "",
            "导航:",
            "  Tab    切换标签页",
            "  q      退出",
        ]
        rows = [_PreviewRow(line) for line in lines]
        self._mode = "help"
        self._show_overlay("快捷键帮助", rows)
        self._update_footer()
