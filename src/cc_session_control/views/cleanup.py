"""Cleanup Tab — session statistics and prune/sweep operations."""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static

from ..data.sessions import cleanup_stats, prune_sessions, remove_session, scan


class CleanupView(Container):
    BINDINGS = [
        Binding("p", "prune_empty", "清理空壳(0提问)", show=True),
        Binding("shift+p", "prune_short", "清理短会话(≤2)", show=True),
        Binding("r", "refresh", "刷新", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._stats: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield Static("扫描中…", id="cleanup-stats")
        yield Static("", id="cleanup-result")

    def on_mount(self) -> None:
        self.load_stats()

    @work(thread=True)
    def load_stats(self) -> None:
        sessions = scan()
        stats = cleanup_stats(sessions)
        self.app.call_from_thread(self._update_stats, stats)

    def _update_stats(self, stats: dict[str, int]) -> None:
        self._stats = stats
        panel = self.query_one("#cleanup-stats", Static)
        panel.update(
            f"会话统计\n"
            f"  总会话:        {stats['total']}\n"
            f"  空壳(0提问):   {stats['empty']}\n"
            f"  短会话(≤2):    {stats['short']}\n"
            f"  孤儿目录:      {stats['orphans']}\n\n"
            f"p 清理空壳 · P 清理≤2提问 · r 刷新"
        )

    @work(thread=True)
    def _do_prune(self, max_prompts: int) -> None:
        sessions = scan()
        targets = prune_sessions(sessions, max_prompts=max_prompts)
        count = len(targets)
        for s in targets:
            remove_session(s)
        stats = cleanup_stats(scan())
        self.app.call_from_thread(self._show_result, count, stats)

    def _show_result(self, count: int, stats: dict[str, int]) -> None:
        result = self.query_one("#cleanup-result", Static)
        result.update(f"已清理 {count} 条会话")
        self._update_stats(stats)

    def action_prune_empty(self) -> None:
        self._do_prune(0)

    def action_prune_short(self) -> None:
        self._do_prune(2)

    def action_refresh(self) -> None:
        self.load_stats()
