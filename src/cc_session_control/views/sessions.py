"""Sessions view — urwid ListBox with keyboard actions and cleanup submenu."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

import urwid

from ..actions.session_ops import (
    relaunch_in_tmux,
    resume_cmd,
    terminate_session,
    to_clipboard,
)
from ..data import liveness, proc, registry
from ..data.cleanup import (
    cleanup_classified,
    list_aged_entries,
    list_orphan_dirs,
    prune_sessions,
    remove_aged_entries,
    remove_orphan_dirs,
    remove_session,
    remove_zombie_session_files,
    select_zombie_pids,
)
from ..data.sessions import scan
from ..models import AgentJob, Session, SessionProc
from ._session_row import (
    _SESSION_HEADER,
    SessionRow,
    _ActionRow,
    _hidden_marker,
    _PreviewRow,
)

if TYPE_CHECKING:
    from ..data.snapshot import WorldSnapshot

    from ..app import App

# R10/D7: refusal shown when the "current" session can't be determined (no /proc)
# — session-keyed destructive ops are disabled rather than silently doing nothing.
_DEGRADED = "liveness 降级：破坏性操作已禁用"

# Submenu actions. `stat` keys index `cleanup_classified`. The age sweep
# (Strategy B) is mtime-only/session-agnostic, so it is NOT R10-gated; every
# other action is.
_CLEANUP_ACTIONS = [
    {"key": "empty",   "label": "空壳会话(0提问)",      "stat": "empty",        "gated": True},
    {"key": "short",   "label": "短会话(≤2提问)",       "stat": "short",        "gated": True},
    {"key": "orphans", "label": "孤儿目录(sid 键)",      "stat": "orphan_dirs",  "gated": True},
    {"key": "zombies", "label": "僵尸会话文件(pid 键)",  "stat": "zombie_procs", "gated": True},
    {"key": "aged",    "label": "过期全局文件(按天)",    "stat": "aged_entries", "gated": False},
]
_GATED_ACTIONS = {a["key"] for a in _CLEANUP_ACTIONS if a["gated"]}


class SessionsView:
    # mode: "list" | "filter" | "cleanup" | "preview"
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
        hidden = "h 隐藏桥接项" if self._show_hidden else "h 显示桥接项"
        return (
            f"Enter 接回 · s 终止 · f 分叉 · d 删除 · y 复制 · R tmux化 · "
            f"c 清理 · {hidden} · / 过滤 · ? 帮助"
        )

    def _update_footer(self) -> None:
        if self.app.views[self.app._active] is not self:
            return
        hints = self.keyhints()
        self.app.footer_text.set_text(f" Tab 切换 · q 退出 · {hints}")

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
            empty = "无匹配会话（按 / 清空过滤）" if self._filter_text else "暂无会话"
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
            f" 共 {len(self._all_sessions)} 条会话 · 活 {alive_n} · 显示 {len(self._sessions)}{flt}{hidden_text}{cleanup_text}"
        )

    def _rebuild_cleanup(self) -> None:
        c = self._classified
        self._cleanup_walker.clear()
        for a in _CLEANUP_ACTIONS:
            count = c.get(a["stat"], 0)
            self._cleanup_walker.append(_ActionRow(a["key"], a["label"], count))

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
        # badge and the `h` toggle stay consistent regardless of which signal
        # flagged the session.
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

    # --- Cleanup submenu ---

    def _enter_cleanup(self) -> None:
        self._mode = "cleanup"
        self._rebuild_cleanup()
        cleanup_list = urwid.ListBox(self._cleanup_walker)
        title = urwid.AttrMap(urwid.Text(" 清理会话", align="center"), "col_header")
        box_content = urwid.Frame(cleanup_list, header=title)
        box = urwid.LineBox(box_content)
        overlay = urwid.Overlay(
            box, self._list_body,
            align="center", width=("relative", 50),
            valign="middle", height=min(len(self._cleanup_walker) + 4, 20),
        )
        self._body.original_widget = overlay
        self._update_footer()

    def _exit_cleanup(self) -> None:
        self._mode = "list"
        self._body.original_widget = self._list_body
        self._update_footer()

    def _selected_action(self) -> str | None:
        if not self._cleanup_walker:
            return None
        widget = self._cleanup_walker.get_focus()[0]
        if isinstance(widget, _ActionRow):
            return widget.action_key
        return None

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

    def _open_preview(self, action: str, title: str, rows: list) -> None:
        """Shared preview-overlay entry for a dir/file sweep (no session list)."""
        self._mode = "preview"
        self._preview_action = action
        self._preview_sessions = []
        self._show_overlay(title, rows)
        self._update_footer()

    def _enter_preview(self, action: str) -> None:
        # R10/D7: session-keyed destructive sweeps need a determinable "current"
        # (without /proc every pid looks dead, so they'd nuke the live session).
        # Refuse HONESTLY — never let the refusal read as "nothing to clean".
        if action in _GATED_ACTIONS and not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return

        if action in ("empty", "short"):
            sessions = scan()
            if action == "empty":
                targets = prune_sessions(sessions, max_prompts=0)
                label = "空壳会话"
            else:
                targets = [s for s in prune_sessions(sessions, max_prompts=2) if s.prompts > 0]
                label = "短会话(≤2提问)"
            if not targets:
                self.app.notify(f"无{label}需要清理")
                return
            self._mode = "preview"
            self._preview_action = action
            self._preview_sessions = targets
            rows = []
            for s in targets:
                when = time.strftime("%m-%d %H:%M", time.localtime(s.mtime))
                cwd = s.cwd.rstrip("/").rsplit("/", 1)[-1] if s.cwd else ""
                line = f"{when}  p{s.prompts}  {s.label[:60]}  ({cwd})"
                rows.append(_PreviewRow(line))
            self._show_overlay(f"将清理 {len(targets)} 条{label}", rows)
            self._update_footer()
        elif action == "orphans":
            orphan_paths = list_orphan_dirs(scan())
            if not orphan_paths:
                self.app.notify("无孤儿目录需要清理")
                return
            rows = [_PreviewRow(p) for p in orphan_paths]
            self._open_preview(action, f"将清理 {len(orphan_paths)} 个孤儿目录", rows)
        elif action == "zombies":
            pids = select_zombie_pids(self._session_procs, self._cur)
            if not pids:
                self.app.notify("无僵尸会话文件需要清理")
                return
            rows = [_PreviewRow(f"sessions/{pid}.json") for pid in pids]
            self._open_preview(action, f"将清理 {len(pids)} 个僵尸会话文件", rows)
        elif action == "aged":
            entries = list_aged_entries()
            if not entries:
                self.app.notify("无过期文件需要清理")
                return
            rows = [_PreviewRow(e) for e in entries]
            self._open_preview(action, f"将清理 {len(entries)} 个过期项", rows)

    def _confirm_cleanup(self) -> None:
        action = self._preview_action
        if action in ("empty", "short"):
            removed = sum(1 for t in self._preview_sessions if remove_session(t))
            self.app.notify(f"已清理 {removed} 条会话")
        elif action == "orphans":
            count = remove_orphan_dirs(scan())
            self.app.notify(f"已清理 {count} 个孤儿目录")
        elif action == "zombies":
            count = remove_zombie_session_files(self._session_procs, self._cur)
            self.app.notify(f"已清理 {count} 个僵尸会话文件")
        elif action == "aged":
            count = remove_aged_entries()
            self.app.notify(f"已清理 {count} 个过期项")
        self._preview_action = None
        self._preview_sessions = []
        self._enter_cleanup()
        self.app.trigger_async_refresh()

    def _do_terminate(self, s: Session) -> None:
        """Terminate body, run only after the y/n confirm accepts."""
        ok = terminate_session(s)
        self.app.notify("已终止" if ok else "终止失败")
        self.app.trigger_async_refresh()

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
            self.app.exit_with_resume(s, fork=False)
        elif key == "f" and s:
            self.app.exit_with_resume(s, fork=True)
        elif key == "s" and s:
            # Guards run BEFORE the confirm — never ask to confirm an invalid op.
            if not s.alive:
                self.app.notify("会话不是活的")
                return
            if s.current:
                self.app.notify("不能终止当前会话")
                return
            self.app.confirm(
                f"终止会话「{s.label[:30]}」？", lambda: self._do_terminate(s)
            )
        elif key == "R" and s:
            if s.current:
                self.app.notify("不能搬动当前会话")
                return
            ok = relaunch_in_tmux(s)
            self.app.notify("已搬进 tmux + 远控（手机/网页可接管）" if ok else "搬入 tmux 失败")
            self.app.trigger_async_refresh()
        elif key == "d" and s:
            if s.alive:
                self.app.notify("活会话不删，先终止")
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
            "会话操作:",
            "  Enter  接回选中的会话（在终端中恢复）",
            "  f      分叉会话（创建副本后接回）",
            "  s      终止活跃会话（发送 SIGTERM，需二次确认）",
            "  R      搬进 tmux 并开启远程控制（脱离终端，手机/网页可接管）",
            "  d      删除已结束的会话记录",
            "  y      复制接回命令到剪贴板",
            "  h      显示/隐藏桥接、SDK 会话",
            "",
            "清理与过滤:",
            "  c      打开清理子菜单",
            "  /      按关键词过滤会话列表",
            "  r      刷新数据",
            "",
            "导航:",
            "  Tab    切换标签页",
            "  q      退出",
        ]
        rows = [_PreviewRow(line) for line in lines]
        self._mode = "help"
        self._show_overlay("快捷键帮助", rows)
        self._update_footer()
