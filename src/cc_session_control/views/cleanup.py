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
        Binding("p", "prune_empty", "Prune empty (0 prompts)", show=True),
        Binding("shift+p", "prune_short", "Prune short (<=2)", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._stats: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield Static("Scanning...", id="cleanup-stats")
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
            f"Session Statistics\n"
            f"  Total sessions:     {stats['total']}\n"
            f"  Empty (0 prompts):  {stats['empty']}\n"
            f"  Short (<=2):        {stats['short']}\n"
            f"  Orphan directories: {stats['orphans']}\n\n"
            f"Press p to prune empty · P to prune <=2 · r to refresh"
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
        result.update(f"Pruned {count} session(s)")
        self._update_stats(stats)

    def action_prune_empty(self) -> None:
        self._do_prune(0)

    def action_prune_short(self) -> None:
        self._do_prune(2)

    def action_refresh(self) -> None:
        self.load_stats()
