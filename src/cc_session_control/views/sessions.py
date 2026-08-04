"""Sessions view — urwid ListBox with keyboard actions and cleanup submenu."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import urwid

from ..actions import tui_actions
from ..actions.session_ops import (
    ResumeIntent,
    attach_target,
    would_take_over,
)
from ..data import proc, providers
from ..data.cleanup import CleanupPlan
from ..models import InventoryIssue, Session, issue_detail
from ._base import ListTabView
from ._confirm import (
    accept_ancestor_probe,
    confirm_stop,
    confirm_takeover,
    confirm_tmux_takeover,
)
from ._keytable import HelpLayout, Key, footer_hints, help_lines
from ._rows import TextRow
from ._session_row import (
    _SESSION_HEADER,
    SessionRow,
    _hidden_marker,
)
from ._sessions_cleanup import CleanupMixin, _CleanupPreview

if TYPE_CHECKING:
    from ..app import App
    from ..data.refresh import RefreshBatch, RefreshFailure


class SessionsView(CleanupMixin, ListTabView):
    # mode: "list" | "filter" | "cleanup" | "preview" | "help"

    OVERLAY_WIDTH = 70

    # Single source for every list-mode key: footer hints, help overlay, and
    # dispatch are all generated from this table (views/_keytable.py). Full key
    # table in the footer is a user preference (2026-07-05); `r 刷新` stays in
    # the App-level FOOTER_PREFIX, so its entry is hint-less here.
    KEY_TABLE = (
        Key(
            ("enter",),
            "Enter 接回",
            "_key_resume",
            section="会话操作:",
            help_lines=(
                "  Enter  tmux 接回（主操作：会话恢复进所属项目的 tmux 窗口并接入前台，",
                "         终端断线会话不死；已驻留 tmux 的会话就地进入不重启；",
                "         接运行中的裸终端会话会先确认接管）",
            ),
        ),
        Key(
            ("t",),
            "t 终端接回",
            "_key_terminal",
            section="会话操作:",
            help_lines=(
                "  t      终端接回（在当前终端恢复，会话随终端关闭而结束——tmux 不可用",
                "         时的兜底；对已驻留会话 = 拉出 tmux，先确认接管）",
            ),
        ),
        Key(
            ("f",),
            "f 分叉",
            "_key_fork",
            section="会话操作:",
            help_lines=(
                "  f      分叉会话（创建副本进 tmux 窗口并进入，不影响原会话）",
            ),
        ),
        Key(
            ("s",),
            "s 停止",
            "_key_stop",
            section="会话操作:",
            help_lines=("  s      停止运行中的会话（发送 SIGTERM，需二次确认）",),
        ),
        Key(
            ("R",),
            "R 转后台",
            "_key_relaunch",
            section="会话操作:",
            help_lines=(
                "  R      转入 tmux 后台（不开远控、不进入，留在 csctl；已驻留会话",
                "         无需转移；接运行中的会话会先确认接管）",
            ),
        ),
        Key(
            ("d",),
            "d 删除",
            "_key_delete",
            section="会话操作:",
            help_lines=("  d      删除已结束的会话记录",),
        ),
        Key(
            ("y",),
            "y 复制命令",
            "_key_yank",
            section="会话操作:",
            help_lines=("  y      复制接回命令到剪贴板",),
        ),
        Key(
            ("h",),
            "h 桥接显隐",
            "_key_toggle_hidden",
            needs_selection=False,
            section="会话操作:",
            help_lines=("  h      显示/隐藏桥接、SDK 会话",),
        ),
        Key(
            ("c",),
            "c 清理",
            "_enter_cleanup",
            needs_selection=False,
            section="清理与过滤:",
            help_lines=("  c      打开清理子菜单",),
        ),
        Key(
            ("/",),
            "/ 过滤",
            "_enter_filter",
            needs_selection=False,
            section="清理与过滤:",
            help_lines=("  /      按关键词过滤会话列表",),
        ),
        Key(
            ("r",),
            None,
            "_key_refresh",
            needs_selection=False,
            section="清理与过滤:",
            help_lines=("  r      刷新",),
        ),
        Key(("?",), "? 详细说明", "_show_help", needs_selection=False),
    )

    HELP_LAYOUT = HelpLayout(
        prefix=(
            "状态列: ● 忙 = 正在生成/执行工具 · ● 闲 = 等待输入 ·",
            "        ○ 停 = 无进程（cx/km 行为「未发现可绑定进程」）",
            "        ▸ = 当前会话（启动 csctl 的会话，受保护） · 📱 = 已开远控",
            "        ⧉ = tmux 驻留（会话进程在 tmux 窗口里，断线不死）",
            "        ? = tmux 驻留未知（盘点不完整，不能确认驻留或裸终端）",
            "CLI 列: cc = Claude Code · cx = Codex · km = Kimi Code",
            "        （codex/kimi 仅精确绑定按 id resume 派发的会话进程；",
            "        launcher 新建与裸启动的进程均不绑定、不会被停止/",
            "        接管，见 ADR-0005）",
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
        self._mode = "list"
        self._filter_text = ""
        self._cleanup_stats: dict[str, int] = {}
        self._classified: dict[str, int] = {}
        self._preview: _CleanupPreview | None = None
        self._provider_issues: tuple[InventoryIssue, ...] = ()
        self._show_hidden = True
        # The frozen cleanup plan (R11/D8 — built from the shared snapshot,
        # never re-scanned per view): counts and each new preview read it;
        # confirmation uses the plan pinned when that preview was rendered.
        self._plan = CleanupPlan()
        self._cleanup_walker = urwid.SimpleFocusListWalker([])

    def keyhints(self) -> str:
        if self._overlay_active():
            # "其余" is honest: the prefix's Tab/q stay global (Tab switches
            # tabs, q QUITS — neither returns to the list).
            return "其余任意键返回"
        if self._mode == "filter":
            return "输入关键词 · Enter 应用过滤 · Esc 取消"
        if self._mode == "cleanup":
            return "Enter 预览待清理项 · Esc 返回会话列表"
        if self._mode == "preview":
            return "Enter 确认清理 · Esc 取消"
        # Every list-mode key gets a brief hint, straight from KEY_TABLE; the
        # footer Text wraps (urwid wrap='space'), trading rows for width.
        return footer_hints(self.KEY_TABLE)

    def apply_refresh(self, batch: RefreshBatch) -> None:
        """Apply one complete generation on the urwid main loop."""
        self._all_sessions = list(batch.snapshot.sessions)
        self._provider_issues = tuple(batch.snapshot.provider_issues)
        self._plan = batch.cleanup_plan
        self._classified = dict(batch.cleanup_counts)
        self._cleanup_stats = dict(batch.session_stats)
        self._loaded = True
        if self._mode == "list" or self._mode == "filter":
            self._apply_filter()
            self._rebuild()
        elif self._mode == "cleanup":
            self._rebuild_cleanup()

    def apply_refresh_failure(self, failure: RefreshFailure) -> None:
        """Apply only the worker-built, session-agnostic cleanup projection."""
        self._apply_failure_cleanup_plan(failure.cleanup_plan)

    def _build_rows(self) -> None:
        for s in self._sessions:
            self.walker.append(SessionRow(s))
        if not self._sessions:
            empty = (
                "无匹配 · 按 / 改过滤 · Esc 清空" if self._filter_text else "暂无会话"
            )
            self.walker.append(urwid.AttrMap(urwid.Text(f" {empty}"), "dead"))

    def _status_text(self) -> str:
        alive_n = sum(1 for s in self._all_sessions if s.alive)
        provider_text = ""
        by_provider: dict[str, int] = {}
        for s in self._all_sessions:
            by_provider[s.provider] = by_provider.get(s.provider, 0) + 1
        if len(by_provider) > 1:
            provider_text = " · " + " ".join(
                f"{providers.get(key).label} {count}"
                for key, count in sorted(by_provider.items())
            )
        degraded_text = ""
        if self._provider_issues:
            degraded_text = (
                f" · 外部源降级 {len(self._provider_issues)}"
                f"（{issue_detail(self._provider_issues[:1])}）"
            )
        flt = f" · 过滤「{self._filter_text}」" if self._filter_text else ""
        empty = self._cleanup_stats.get("empty", 0)
        short = self._cleanup_stats.get("short", 0)
        orphans = self._cleanup_stats.get("orphans", 0)
        cleanup_text = ""
        hidden_n = sum(1 for s in self._all_sessions if s.bridge_or_sdk)
        hidden_text = ""
        tmux_unknown = [
            s
            for s in self._all_sessions
            if s.alive and not s.tmux_target and not s.tmux_inventory_complete
        ]
        tmux_text = ""
        if tmux_unknown:
            detail = next(
                (
                    s.tmux_inventory_detail
                    for s in tmux_unknown
                    if s.tmux_inventory_detail
                ),
                "",
            )
            tmux_text = f" · tmux 驻留未知 {len(tmux_unknown)}"
            if detail:
                tmux_text += f"（{detail}）"
        if hidden_n:
            hidden_text = (
                f" · 桥接/SDK {hidden_n}"
                if self._show_hidden
                else f" · 桥接/SDK已隐藏 {hidden_n}"
            )
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
            f" 共 {len(self._all_sessions)} 条会话{provider_text} · 运行 {alive_n}"
            f" · 显示 {len(self._sessions)}"
            f"{flt}{hidden_text}{tmux_text}{degraded_text}{cleanup_text}"
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
            s for s in self._all_sessions if self._show_hidden or not s.bridge_or_sdk
        ]
        if not self._filter_text:
            self._sessions = visible
        else:
            k = self._filter_text.lower()
            self._sessions = [
                s
                for s in visible
                if k
                in (
                    s.label
                    + " "
                    + s.cwd
                    + " "
                    + s.sid
                    + " "
                    + s.provider
                    + " "
                    + providers.get(s.provider).label
                    + " "
                    + _hidden_marker(s)
                    + " "
                    + " ".join(sorted(s.hidden))
                ).lower()
            ]

    def _enter_filter(self) -> None:
        # The Edit lives in the VIEW's own frame footer (the status-bar slot),
        # not the App footer: notify/set_hints can never evict it, and it stays
        # visible with the list while typing.
        self._mode = "filter"
        self._filter_edit = urwid.Edit("过滤: ")
        self.widget.footer = urwid.AttrMap(self._filter_edit, "notify")
        self._update_footer()

    def captures_text(self) -> bool:
        """While the filter Edit is up it owns every key (incl. tab/q)."""
        return self._mode == "filter"

    def _exit_filter(self, cancel: bool = False) -> None:
        self._mode = "list"
        if cancel:
            self._filter_text = ""
        else:
            self._filter_text = self._filter_edit.get_edit_text()
        self._apply_filter()
        self._rebuild()
        self.widget.footer = self.status
        self._update_footer()

    def _do_terminate(self, s: Session) -> None:
        """Stop body, run only after the y/n confirm accepts."""
        self.app.submit_action(
            "session.stop",
            lambda: tui_actions.stop_session(s),
        )

    def _do_relaunch(self, s: Session) -> None:
        """转后台 body (after confirm when it takes over a live one): spawn the
        resume window in the per-project tmux session, do NOT enter it — the
        operator stays in csctl. No --remote-control (ADR-0001)."""
        self.app.submit_action(
            "session.background",
            lambda: tui_actions.background_session(s),
        )

    def _submit_ancestor_probe(
        self,
        action_key: str,
        on_complete: Callable[[proc.AncestorProbe], None],
    ) -> None:
        """Prepare current-session protection off-loop for one key action."""
        self.app.submit_completion(
            action_key,
            proc.probe_current_ancestors,
            on_complete,
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
        """Enter — tmux 接回: enter a resident session in place, else resume
        it inside its per-project tmux window and enter (ADR-0001 primary)."""
        if s.current:
            self.app.notify("不能接回当前会话")
            return
        if attach_target(s) or not would_take_over(s):
            confirm_tmux_takeover(self.app, s, "接回会话", gated=False)
            return
        self._submit_ancestor_probe(
            "session.resume.prepare",
            lambda evidence: confirm_tmux_takeover(
                self.app,
                s,
                "接回会话",
                evidence=evidence,
            ),
        )

    def _key_terminal(self, s: Session) -> None:
        """t — 终端接回 (fallback): bare-terminal resume; a resident session is
        pulled OUT of tmux via the same standard takeover confirm."""
        if s.current:
            self.app.notify("不能接回当前会话")
            return
        if not would_take_over(s):
            confirm_takeover(
                self.app,
                s,
                "终端接回会话",
                lambda: self.app.exit_with(ResumeIntent(s, fork=False)),
                gated=False,
            )
            return
        self._submit_ancestor_probe(
            "session.terminal.prepare",
            lambda evidence: confirm_takeover(
                self.app,
                s,
                "终端接回会话",
                lambda: self.app.exit_with(ResumeIntent(s, fork=False)),
                evidence=evidence,
            ),
        )

    def _key_fork(self, s: Session) -> None:
        """f — 分叉进 tmux: a fork is a copy (never kills, no confirm).

        Capability-gated (ADR-0005): kimi has no CLI fork argv, so the verb
        is refused with a reason instead of synthesizing a broken command."""
        if not providers.get(s.provider).caps.fork:
            self.app.notify(f"{s.provider} 不支持从命令行分叉会话")
            return
        if s.current:
            self.app.notify("不能分叉当前会话")
            return
        confirm_tmux_takeover(
            self.app,
            s,
            "分叉会话",
            fork=True,
            gated=False,
        )

    def _key_stop(self, s: Session) -> None:
        def complete(evidence: proc.AncestorProbe | None = None) -> None:
            confirm_stop(
                self.app,
                "会话",
                s.label,
                lambda: self._do_terminate(s),
                alive=s.alive,
                current=s.current,
                gated=evidence is not None,
                evidence=evidence,
            )

        if not s.alive:
            complete()
            return
        self._submit_ancestor_probe("session.stop.prepare", complete)

    def _key_relaunch(self, s: Session) -> None:
        """R — 转后台 (no Remote Control): a resident session needs no move."""
        if s.current:
            self.app.notify("不能转入后台当前会话")
            return
        if s.alive and s.tmux_target:
            self.app.notify(f"已在 tmux（{s.tmux_target}），无需转移")
            return
        if not would_take_over(s):
            confirm_takeover(
                self.app,
                s,
                "转入后台",
                lambda: self._do_relaunch(s),
                gated=False,
            )
            return
        self._submit_ancestor_probe(
            "session.background.prepare",
            lambda evidence: confirm_takeover(
                self.app,
                s,
                "转入后台",
                lambda: self._do_relaunch(s),
                evidence=evidence,
            ),
        )

    def _complete_delete(
        self,
        evidence: proc.AncestorProbe,
        s: Session,
    ) -> None:
        if not accept_ancestor_probe(self.app, evidence):
            return
        self.app.submit_action(
            "session.delete",
            lambda: tui_actions.delete_session(s),
        )

    def _key_delete(self, s: Session) -> None:
        if not providers.get(s.provider).caps.cleanup:
            # ADR-0005: csctl never deletes state it does not fully model —
            # a codex/kimi row's file anchor points into that CLI's own store.
            self.app.notify(f"{s.provider} 会话由其 CLI 自己管理，csctl 不删除")
            return
        if s.alive:
            self.app.notify("运行中的会话不删，先停止")
            return
        self._submit_ancestor_probe(
            "session.delete.prepare",
            lambda evidence: self._complete_delete(evidence, s),
        )

    def _key_yank(self, s: Session) -> None:
        self.app.submit_action(
            "session.copy-command",
            lambda: tui_actions.copy_resume_command(s),
        )

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
