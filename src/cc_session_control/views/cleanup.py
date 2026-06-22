"""Cleanup view — selectable action list with preview before execution."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import urwid

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

_ACTIONS = [
    {"key": "empty",   "label": "清理空壳会话(0提问)",  "stat": "empty"},
    {"key": "short",   "label": "清理短会话(≤2提问)",   "stat": "short"},
    {"key": "orphans", "label": "清理孤儿目录",         "stat": "orphans"},
]


class _ActionRow(urwid.WidgetWrap):
    def __init__(self, action_key: str, label: str, count: int) -> None:
        self.action_key = action_key
        cols = urwid.Columns([
            ("weight", 1, urwid.Text(f"  {label}")),
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
        mapped = urwid.AttrMap(urwid.Text(f"  {text}"), "dead", focus_map={"dead": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class CleanupView:
    def __init__(self, app: App) -> None:
        self.app = app
        self._stats: dict[str, int] = {}
        self._pending_stats: dict[str, int] | None = None
        self._loaded = False
        self._previewing = False
        self._preview_action: str | None = None
        self._preview_sessions: list[Session] = []

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        body = urwid.AttrMap(self.listbox, {None: "body"})
        self.widget = urwid.Frame(body, footer=self.status)

    def keyhints(self) -> str:
        if self._previewing:
            return "Enter 确认清理 · Esc 返回"
        return "Enter 预览 · r 刷新"

    def load(self) -> None:
        sessions = scan()
        self._stats = cleanup_stats(sessions)
        self._loaded = True
        self._rebuild()

    def set_pending_stats(self, stats: dict[str, int]) -> None:
        self._pending_stats = stats

    def apply_data(self) -> None:
        if self._pending_stats is not None:
            self._stats = self._pending_stats
            self._pending_stats = None
            self._loaded = True
            if not self._previewing:
                self._rebuild()

    def _rebuild(self) -> None:
        s = self._stats
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        for a in _ACTIONS:
            count = s.get(a["stat"], 0)
            self.walker.append(_ActionRow(a["key"], a["label"], count))
        if focus_pos is not None and focus_pos < len(self.walker):
            self.walker.set_focus(focus_pos)
        total = s.get("total", 0)
        empty = s.get("empty", 0)
        orphans = s.get("orphans", 0)
        self.status.original_widget.set_text(f" 总 {total} 会话 · 空壳 {empty} · 孤儿 {orphans}")

    def _update_footer(self) -> None:
        if self.app.views[self.app._active] is not self:
            return
        hints = self.keyhints()
        self.app.footer_text.set_text(f" Tab 切换 · q 退出 · {hints}")

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
            self._previewing = True
            self._preview_action = action
            self._preview_sessions = []
            self.walker.clear()
            for p in orphan_paths:
                self.walker.append(_PreviewRow(p))
            self.status.original_widget.set_text(f" 将清理 {len(orphan_paths)} 个孤儿目录 · Enter 确认 · Esc 返回")
            self._update_footer()
            return
        else:
            return

        if not targets:
            self.app.notify(f"无{label}需要清理")
            return

        self._previewing = True
        self._preview_action = action
        self._preview_sessions = targets
        self.walker.clear()
        for s in targets:
            when = time.strftime("%m-%d %H:%M", time.localtime(s.mtime))
            cwd = s.cwd.rstrip("/").rsplit("/", 1)[-1] if s.cwd else ""
            line = f"{when}  p{s.prompts}  {s.label[:60]}  ({cwd})"
            self.walker.append(_PreviewRow(line))
        self.status.original_widget.set_text(
            f" 将清理 {len(targets)} 条{label} · Enter 确认 · Esc 返回"
        )
        self._update_footer()

    def _exit_preview(self) -> None:
        self._previewing = False
        self._preview_action = None
        self._preview_sessions = []
        self._rebuild()

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
        self._exit_preview()
        self.app.trigger_async_refresh()

    def handle_key(self, key: str) -> None:
        if self._previewing:
            if key == "enter":
                self._confirm_cleanup()
            elif key == "esc":
                self._exit_preview()
            return

        if key == "enter":
            action = self._selected_action()
            if action:
                self._enter_preview(action)
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
