"""Cleanup view — session statistics and prune operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..data.sessions import cleanup_stats, prune_sessions, remove_session, scan

if TYPE_CHECKING:
    from ..app import App


class CleanupView:
    def __init__(self, app: App) -> None:
        self.app = app
        self._stats: dict[str, int] = {}
        self._pending_stats: dict[str, int] | None = None

        self.stats_text = urwid.Text("扫描中…")
        self.result_text = urwid.Text("")
        self.widget = urwid.Filler(
            urwid.Pile([
                urwid.Text(""),
                self.stats_text,
                urwid.Text(""),
                self.result_text,
            ]),
            valign="top",
        )

    def keyhints(self) -> str:
        return "p 清理空壳 · P 清理≤2提问"

    def load(self) -> None:
        sessions = scan()
        self._stats = cleanup_stats(sessions)
        self._update_display()

    def refresh_data(self) -> None:
        sessions = scan()
        self._pending_stats = cleanup_stats(sessions)

    def apply_data(self) -> None:
        if self._pending_stats is not None:
            self._stats = self._pending_stats
            self._pending_stats = None
            self._update_display()

    def _update_display(self) -> None:
        s = self._stats
        self.stats_text.set_text(
            f"  会话统计\n"
            f"    总会话:        {s.get('total', 0)}\n"
            f"    空壳(0提问):   {s.get('empty', 0)}\n"
            f"    短会话(≤2):    {s.get('short', 0)}\n"
            f"    孤儿目录:      {s.get('orphans', 0)}\n\n"
            f"  p 清理空壳 · P 清理≤2提问 · r 刷新"
        )

    def _do_prune(self, max_prompts: int) -> None:
        sessions = scan()
        targets = prune_sessions(sessions, max_prompts=max_prompts)
        count = len(targets)
        for s in targets:
            remove_session(s)
        self.result_text.set_text(f"  已清理 {count} 条会话")
        self.load()

    def handle_key(self, key: str) -> None:
        if key == "p":
            self._do_prune(0)
        elif key == "P":
            self._do_prune(2)
        elif key == "r":
            self.load()
            self.app.notify("已刷新")
