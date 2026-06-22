"""Cleanup view — selectable action list, consistent with sessions/RC tabs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..data.agents import invalidate_cache
from ..data.sessions import cleanup_stats, prune_sessions, remove_orphan_dirs, remove_session, scan

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
        attr = "dead"
        mapped = urwid.AttrMap(cols, attr, focus_map={"dead": "selected", None: "selected"})
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

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        body = urwid.AttrMap(self.listbox, {None: "body"})
        self.widget = urwid.Frame(body, header=self.status)

    def keyhints(self) -> str:
        return "Enter 执行 · r 刷新"

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

    def _selected(self) -> str | None:
        if not self.walker:
            return None
        widget = self.walker.get_focus()[0]
        if isinstance(widget, _ActionRow):
            return widget.action_key
        return None

    def _do_prune(self, max_prompts: int) -> None:
        sessions = scan()
        targets = prune_sessions(sessions, max_prompts=max_prompts)
        count = len(targets)
        for t in targets:
            remove_session(t)
        self.app.notify(f"已清理 {count} 条会话")
        invalidate_cache()
        self.app.trigger_async_refresh()

    def _do_orphan_cleanup(self) -> None:
        sessions = scan()
        count = remove_orphan_dirs(sessions)
        self.app.notify(f"已清理 {count} 个孤儿目录")
        self.app.trigger_async_refresh()

    def handle_key(self, key: str) -> None:
        if key == "enter":
            action = self._selected()
            if action == "empty":
                self._do_prune(0)
            elif action == "short":
                self._do_prune(2)
            elif action == "orphans":
                self._do_orphan_cleanup()
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
