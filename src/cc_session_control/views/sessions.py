"""Sessions view — urwid ListBox with keyboard actions and cleanup submenu."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import urwid

from ..actions.session_ops import resume_cmd, terminate_session, to_clipboard
from ..data.agents import invalidate_cache
from ..data.sessions import (
    cleanup_stats,
    list_orphan_dirs,
    prune_sessions,
    remove_orphan_dirs,
    remove_session,
    scan,
)
from ..models import Session

if TYPE_CHECKING:
    from ..app import App

_CLEANUP_ACTIONS = [
    {"key": "empty",   "label": "空壳会话(0提问)",  "stat": "empty"},
    {"key": "short",   "label": "短会话(≤2提问)",   "stat": "short"},
    {"key": "orphans", "label": "孤儿目录",         "stat": "orphans"},
]


class SessionRow(urwid.WidgetWrap):
    def __init__(self, session: Session) -> None:
        self.session = session
        mark = "●" if session.alive else "○"
        cur = "▸" if session.current else " "
        when = time.strftime("%m-%d %H:%M", time.localtime(session.mtime))
        label = session.label[:80]
        cwd = session.cwd.rstrip("/").rsplit("/", 1)[-1] if session.cwd else ""

        cols = urwid.Columns([
            (3, urwid.Text(f"{cur}{mark}")),
            (12, urwid.Text(when)),
            (5, urwid.Text(f"p{session.prompts}")),
            ("weight", 3, urwid.Text(label, wrap="clip")),
            ("weight", 1, urwid.Text(cwd, wrap="clip")),
        ], min_width=6)

        attr = "alive" if session.alive else "dead"
        mapped = urwid.AttrMap(cols, attr, focus_map={"alive": "selected", "dead": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class _ActionRow(urwid.WidgetWrap):
    def __init__(self, action_key: str, label: str, count: int) -> None:
        self.action_key = action_key
        cols = urwid.Columns([
            ("weight", 1, urwid.Text(label)),
            (8, urwid.Text(str(count), align="right")),
        ])
        mapped = urwid.AttrMap(cols, "dead", focus_map={"dead": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class _PreviewRow(urwid.WidgetWrap):
    def __init__(self, text: str) -> None:
        mapped = urwid.AttrMap(urwid.Text(text), "dead", focus_map={"dead": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


_SESSION_HEADER = urwid.Columns([
    (3, urwid.Text("")),
    (12, urwid.Text("时间")),
    (5, urwid.Text("提问")),
    ("weight", 3, urwid.Text("标题")),
    ("weight", 1, urwid.Text("项目")),
], min_width=6)

_CLEANUP_HEADER = urwid.Columns([
    ("weight", 1, urwid.Text("操作")),
    (8, urwid.Text("数量", align="right")),
])


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
        self._preview_action: str | None = None
        self._preview_sessions: list[Session] = []

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        self._col_header = urwid.WidgetPlaceholder(
            urwid.AttrMap(_SESSION_HEADER, "col_header")
        )
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        body = urwid.AttrMap(self.listbox, {None: "body"})
        self.widget = urwid.Frame(body, header=self._col_header, footer=self.status)

    def keyhints(self) -> str:
        if self._mode == "cleanup":
            return "Enter 预览 · Esc 返回 · r 刷新"
        if self._mode == "preview":
            return "Enter 确认清理 · Esc 返回"
        return "Enter 接回 · f 分叉 · t 终止 · d 删除 · y 复制 · c 清理 · / 过滤"

    def _update_footer(self) -> None:
        if self.app.views[self.app._active] is not self:
            return
        hints = self.keyhints()
        self.app.footer_text.set_text(f" Tab 切换 · q 退出 · {hints}")

    def load(self) -> None:
        sessions = scan()
        self._all_sessions = sessions
        self._sessions = sessions
        self._cleanup_stats = cleanup_stats(sessions)
        self._loaded = True
        self._rebuild()

    def set_pending(self, sessions: list[Session]) -> None:
        self._pending = sessions

    def set_pending_stats(self, stats: dict[str, int]) -> None:
        self._cleanup_stats = stats

    def apply_data(self) -> None:
        if self._pending is not None:
            self._all_sessions = self._pending
            self._pending = None
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
        if self.walker and focus_pos is not None:
            self.walker.set_focus(min(focus_pos, len(self.walker) - 1))
        alive_n = sum(1 for s in self._all_sessions if s.alive)
        flt = f" · 过滤「{self._filter_text}」" if self._filter_text else ""
        empty = self._cleanup_stats.get("empty", 0)
        short = self._cleanup_stats.get("short", 0)
        orphans = self._cleanup_stats.get("orphans", 0)
        cleanup_text = ""
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
            f" 共 {len(self._all_sessions)} 条会话 · 活 {alive_n} · 显示 {len(self._sessions)}{flt}{cleanup_text}"
        )

    def _rebuild_cleanup(self) -> None:
        s = self._cleanup_stats
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        for a in _CLEANUP_ACTIONS:
            count = s.get(a["stat"], 0)
            self.walker.append(_ActionRow(a["key"], a["label"], count))
        if focus_pos is not None and focus_pos < len(self.walker):
            self.walker.set_focus(focus_pos)
        total = s.get("total", 0)
        empty = s.get("empty", 0)
        orphans = s.get("orphans", 0)
        self.status.original_widget.set_text(f" 总 {total} 会话 · 空壳 {empty} · 孤儿 {orphans}")

    def _selected(self) -> Session | None:
        if not self.walker:
            return None
        widget = self.walker.get_focus()[0]
        if isinstance(widget, SessionRow):
            return widget.session
        return None

    def _apply_filter(self) -> None:
        if not self._filter_text:
            self._sessions = self._all_sessions
        else:
            k = self._filter_text.lower()
            self._sessions = [
                s for s in self._all_sessions
                if k in (s.label + " " + s.cwd + " " + s.sid).lower()
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
        self._col_header.original_widget = urwid.AttrMap(_CLEANUP_HEADER, "col_header")
        self._rebuild_cleanup()
        self._update_footer()

    def _exit_cleanup(self) -> None:
        self._mode = "list"
        self._col_header.original_widget = urwid.AttrMap(_SESSION_HEADER, "col_header")
        self._apply_filter()
        self._rebuild()
        self._update_footer()

    def _selected_action(self) -> str | None:
        if not self.walker:
            return None
        widget = self.walker.get_focus()[0]
        if isinstance(widget, _ActionRow):
            return widget.action_key
        return None

    def _enter_preview(self, action: str) -> None:
        sessions = scan()
        if action == "empty":
            targets = prune_sessions(sessions, max_prompts=0)
            label = "空壳会话"
        elif action == "short":
            targets = prune_sessions(sessions, max_prompts=2)
            label = "短会话(≤2提问)"
        elif action == "orphans":
            orphan_paths = list_orphan_dirs(sessions)
            if not orphan_paths:
                self.app.notify("无孤儿目录需要清理")
                return
            self._mode = "preview"
            self._preview_action = action
            self._preview_sessions = []
            self.walker.clear()
            for p in orphan_paths:
                self.walker.append(_PreviewRow(p))
            self.status.original_widget.set_text(f" 将清理 {len(orphan_paths)} 个孤儿目录")
            self._update_footer()
            return
        else:
            return

        if not targets:
            self.app.notify(f"无{label}需要清理")
            return

        self._mode = "preview"
        self._preview_action = action
        self._preview_sessions = targets
        self.walker.clear()
        for s in targets:
            when = time.strftime("%m-%d %H:%M", time.localtime(s.mtime))
            cwd = s.cwd.rstrip("/").rsplit("/", 1)[-1] if s.cwd else ""
            line = f"{when}  p{s.prompts}  {s.label[:60]}  ({cwd})"
            self.walker.append(_PreviewRow(line))
        self.status.original_widget.set_text(f" 将清理 {len(targets)} 条{label}")
        self._update_footer()

    def _confirm_cleanup(self) -> None:
        action = self._preview_action
        if action in ("empty", "short"):
            count = len(self._preview_sessions)
            for t in self._preview_sessions:
                remove_session(t)
            self.app.notify(f"已清理 {count} 条会话")
            invalidate_cache()
        elif action == "orphans":
            sessions = scan()
            count = remove_orphan_dirs(sessions)
            self.app.notify(f"已清理 {count} 个孤儿目录")
        self._preview_action = None
        self._preview_sessions = []
        self._enter_cleanup()
        self.app.trigger_async_refresh()

    # --- Key dispatch ---

    def handle_key(self, key: str) -> None:
        if self._mode == "filter":
            if key == "enter":
                self._exit_filter()
            elif key == "esc":
                self._exit_filter(cancel=True)
            else:
                self.widget.keypress((80,), key)
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
        elif key == "t" and s:
            if not s.alive:
                self.app.notify("会话不是活的")
                return
            if s.current:
                self.app.notify("不能终止当前会话")
                return
            ok = terminate_session(s)
            self.app.notify("已终止" if ok else "终止失败")
            invalidate_cache()
            self.app.trigger_async_refresh()
        elif key == "d" and s:
            if s.alive:
                self.app.notify("活会话不删，先终止")
                return
            remove_session(s)
            self.app.notify("已删除")
            self.app.trigger_async_refresh()
        elif key == "y" and s:
            cmd = resume_cmd(s)
            ok = to_clipboard(cmd)
            self.app.notify("已复制" if ok else f"复制失败: {cmd}")
        elif key == "c":
            self._enter_cleanup()
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
        elif key == "/":
            self._enter_filter()
