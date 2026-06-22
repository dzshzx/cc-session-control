"""Cleanup view — session statistics and prune operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..data.agents import invalidate_cache
from ..data.sessions import cleanup_stats, prune_sessions, remove_session, scan

if TYPE_CHECKING:
    from ..app import App


class _StatRow(urwid.WidgetWrap):
    def __init__(self, label: str, value: str) -> None:
        cols = urwid.Columns([
            ("weight", 1, urwid.Text(f"  {label}")),
            (8, urwid.Text(value, align="right")),
        ])
        super().__init__(cols)

    def selectable(self) -> bool:
        return False


class CleanupView:
    def __init__(self, app: App) -> None:
        self.app = app
        self._stats: dict[str, int] = {}
        self._pending_stats: dict[str, int] | None = None
        self._loaded = False

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        self.widget = urwid.Frame(self.listbox, header=self.status)

    def keyhints(self) -> str:
        return "p 清理空壳 · P 清理≤2提问"

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
        self.walker.clear()
        self.walker.append(urwid.Text(""))
        self.walker.append(_StatRow("总会话", str(s.get("total", 0))))
        self.walker.append(_StatRow("空壳(0提问)", str(s.get("empty", 0))))
        self.walker.append(_StatRow("短会话(≤2)", str(s.get("short", 0))))
        self.walker.append(_StatRow("孤儿目录", str(s.get("orphans", 0))))
        self.walker.append(urwid.Text(""))
        self.walker.append(urwid.Text("  p 清理空壳 · P 清理≤2提问 · r 刷新"))
        self.status.original_widget.set_text(
            f" 总 {s.get('total', 0)} · 空壳 {s.get('empty', 0)} · 孤儿 {s.get('orphans', 0)}"
        )

    def _do_prune(self, max_prompts: int) -> None:
        sessions = scan()
        targets = prune_sessions(sessions, max_prompts=max_prompts)
        count = len(targets)
        for t in targets:
            remove_session(t)
        self.app.notify(f"已清理 {count} 条会话")
        invalidate_cache()
        self.app.trigger_async_refresh()

    def handle_key(self, key: str) -> None:
        if key == "p":
            self._do_prune(0)
        elif key == "P":
            self._do_prune(2)
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
