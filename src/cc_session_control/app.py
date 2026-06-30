"""CCM — Claude Code Manager urwid App."""

from __future__ import annotations

import curses
import os
import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable

import urwid

from .data import proc
from .data.snapshot import WorldSnapshot, build_world_snapshot
from .views.agents import AgentsView
from .views.rc import RCView
from .views.sessions import SessionsView

# D7/R10: shown across all tabs when `/proc` is unavailable (e.g. macOS), where
# the "current" session can't be determined and destructive ops are refused.
_DEGRADED_BANNER = "⚠ liveness 降级（无 /proc）：terminate/delete/cleanup 已受限"


@runtime_checkable
class TabView(Protocol):
    """The contract App uses to drive each tab generically.

    A tab satisfies this structurally — App never special-cases a concrete
    view. `fetch_pending(snapshot)` runs on the worker thread and must not touch
    widgets; `apply_data()` runs on the main loop and swaps `_pending` into the
    walker. The `snapshot` is the shared per-cycle world (R11/D8); it is OPTIONAL
    — a view called with `None` self-fetches (back-compat / tests). Adding a tab
    means honoring every member below.
    """

    widget: urwid.Widget
    _loaded: bool

    def load(self) -> None: ...
    def fetch_pending(self, snapshot: WorldSnapshot | None = None) -> None: ...
    def apply_data(self) -> None: ...
    def keyhints(self) -> str: ...
    def handle_key(self, key: str) -> None: ...

# 6-tuple: (name, fg_16, bg_16, mono, fg_256, bg_256)
PALETTE = [
    ("header",     "white,bold",  "black", "bold",     "#fff,bold",  "#111"),
    ("footer",     "light gray",  "black", None,       "#999",       "#111"),
    ("tab_on",     "white,bold",  "dark cyan", "bold,standout", "#fff,bold", "#068"),
    ("tab_off",    "dark cyan",   "black", None,       "#688",       "#111"),
    ("alive",      "light green", "black", None,       "#6d6",       "#111"),
    ("dead",       "light gray",  "black", None,       "#ccc",       "#111"),
    ("selected",   "white,bold",  "dark cyan", "standout", "#fff,bold", "#068"),
    ("notify",     "yellow,bold", "black", "bold",     "#ff0,bold",  "#111"),
    ("status",     "light gray",  "black", None,       "#aaa",       "#111"),
    ("rc_running", "light green", "black", None,       "#6d6",       "#111"),
    ("rc_stopped", "light gray",  "black", None,       "#ccc",       "#111"),
    ("body",       "light gray",  "black", None,       "#ccc",       "#111"),
    ("col_header", "dark cyan",   "black", None,       "#8aa",       "#181818"),
]

TAB_NAMES = ["会话", "后台", "远程控制"]

# D1: all three tabs share ONE footer prefix — the universal verbs (Tab/q/r) live
# here exactly once so `r 刷新` shows identically on every tab. View-specific keys
# come from each view's `keyhints()` and are appended via `App.set_hints`.
FOOTER_PREFIX = " Tab 切换 · q 退出 · r 刷新 · "


def _make_screen() -> urwid.raw_display.Screen:
    screen = urwid.raw_display.Screen()
    try:
        curses.setupterm()
        term_colors = curses.tigetnum("colors")
        if term_colors >= 256:
            screen.set_terminal_properties(colors=256)
    except Exception:
        pass
    screen.register_palette(PALETTE)
    return screen


class App:
    def __init__(self) -> None:
        self.result: tuple | None = None
        self._exiting = False
        self._alarm_handle: object | None = None
        self._pipe_fd: int | None = None
        self._refreshing = False
        # When set, a y/n confirm modal is up: `_input` routes y/n/esc here and
        # swallows every other key (App-level so all tabs share it — D8 view files
        # stay small). `_confirm_base` is the widget restored on close.
        self._confirm_yes: Callable[[], None] | None = None
        self._confirm_base: urwid.Widget | None = None

        self.views: list[TabView] = [SessionsView(self), AgentsView(self), RCView(self)]
        self._active = 0

        self.body = urwid.WidgetPlaceholder(self.views[0].widget)
        self._tab_texts: list[urwid.Text] = []
        tab_bar = self._build_tab_bar()
        title = urwid.AttrMap(urwid.Text("Claude Code 会话管理器", align="center"), "header")
        # Title at 0 and tab_bar at 1 are positional (see `_update_tab_bar`); the
        # degraded banner, if any, is appended LAST so those indices are stable.
        header_rows: list[urwid.Widget] = [title, tab_bar]
        if not proc.has_proc():
            header_rows.append(urwid.AttrMap(urwid.Text(f" {_DEGRADED_BANNER}"), "notify"))
        self.header = urwid.Pile(header_rows)

        self.footer_text = urwid.Text(FOOTER_PREFIX)
        self.footer = urwid.AttrMap(self.footer_text, "footer")

        self.frame = urwid.Frame(self.body, header=self.header, footer=self.footer)

        self._screen = _make_screen()
        self.loop = urwid.MainLoop(
            self.frame,
            screen=self._screen,
            unhandled_input=self._input,
        )

    def _build_tab_bar(self) -> urwid.Columns:
        self._tab_texts = []
        cols = []
        for i, name in enumerate(TAB_NAMES):
            txt = urwid.Text(f" {name} ", align="center")
            self._tab_texts.append(txt)
            attr = "tab_on" if i == self._active else "tab_off"
            cols.append(urwid.AttrMap(txt, attr))
        return urwid.Columns(cols)

    def _update_tab_bar(self) -> None:
        tab_bar = self._build_tab_bar()
        self.header.contents[1] = (tab_bar, self.header.options())

    def _switch_tab(self) -> None:
        self._active = (self._active + 1) % len(self.views)
        self.body.original_widget = self.views[self._active].widget
        self._update_tab_bar()
        self.set_hints(self.views[self._active].keyhints())
        if not self.views[self._active]._loaded:
            self.trigger_async_refresh()

    def _input(self, key: str) -> None:
        if self._confirm_yes is not None:
            # Modal: only y/n/esc are live; tab/q/everything else is swallowed so
            # a destructive confirm can't be skipped past by an accidental key.
            if key == "y":
                cb = self._confirm_yes
                self._close_confirm()
                cb()
            elif key in ("n", "esc"):
                self._close_confirm()
            return
        if key == "tab":
            self._switch_tab()
        elif key == "q":
            self._exit()
        else:
            self.views[self._active].handle_key(key)

    def confirm(self, message: str, on_yes: Callable[[], None]) -> None:
        """Show a y/n modal over the active tab; run `on_yes` only on `y`.

        App-level (not a per-view mode) so every tab gets confirmation for free
        and the view files stay under budget. While up, `_input` routes y/n/esc
        and swallows the rest. The overlay sits ABOVE the view widget in
        `self.body`, so a worker-thread refresh (which only rebuilds a view's own
        walker) never disturbs it.
        """
        self._confirm_yes = on_yes
        self._confirm_base = self.body.original_widget
        text = urwid.Text(f"  {message}\n\n  y 确认    n / Esc 取消")
        box = urwid.AttrMap(urwid.LineBox(urwid.Filler(text)), "notify")
        self.body.original_widget = urwid.Overlay(
            box, self._confirm_base,
            align="center", width=("relative", 50),
            valign="middle", height=7,
        )
        self.footer_text.set_text(" y 确认 · n/Esc 取消")

    def _close_confirm(self) -> None:
        if self._confirm_base is not None:
            self.body.original_widget = self._confirm_base
        self._confirm_base = None
        self._confirm_yes = None
        self.set_hints(self.views[self._active].keyhints())

    def _exit(self, result: tuple | None = None) -> None:
        self._exiting = True
        if self._alarm_handle:
            self.loop.remove_alarm(self._alarm_handle)
        self.result = result
        raise urwid.ExitMainLoop()

    def exit_with_resume(self, session: object, fork: bool = False) -> None:
        self._exit(("resume", session, fork))

    def set_hints(self, hints: str) -> None:
        """Footer = shared prefix + the active tab's keyhints (D1 single source)."""
        self.footer_text.set_text(FOOTER_PREFIX + hints)

    def notify(self, msg: str, seconds: float = 3) -> None:
        self.frame.footer = urwid.AttrMap(urwid.Text(f" {msg}"), "notify")
        self.loop.set_alarm_in(seconds, lambda *_: self._restore_footer())

    def _restore_footer(self) -> None:
        self.frame.footer = self.footer

    def _run_fetch_cycle(self) -> None:
        """Worker-phase of a refresh — the synchronous, testable seam (R11/D8).

        Computes ONE shared world snapshot per cycle so the three tabs don't each
        re-scan /proc + transcripts, then projects it into every view's `_pending`
        via `fetch_pending(snapshot)`. A failed build degrades to per-view
        self-fetch (`snapshot=None`). Pure data side: it only sets `_pending`
        fields and NEVER touches widgets, so it is safe on the worker thread and
        can be driven directly in tests without a MainLoop.
        """
        try:
            snapshot: WorldSnapshot | None = build_world_snapshot()
        except Exception:
            snapshot = None
        for v in self.views:
            v.fetch_pending(snapshot)

    def trigger_async_refresh(self) -> None:
        if self._refreshing or self._exiting:
            return
        self._refreshing = True

        def worker() -> None:
            try:
                self._run_fetch_cycle()
            finally:
                self._refreshing = False
            if self._pipe_fd is not None:
                try:
                    os.write(self._pipe_fd, b"1")
                except OSError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_refresh(self, loop: object = None, data: object = None) -> None:
        if self._exiting:
            return
        self.trigger_async_refresh()
        self._alarm_handle = self.loop.set_alarm_in(10, self._schedule_refresh)

    def _on_pipe(self, data: bytes) -> bool:
        if not self._exiting:
            for view in self.views:
                view.apply_data()
        return True

    def run(self) -> tuple | None:
        self._pipe_fd = self.loop.watch_pipe(self._on_pipe)
        self.views[self._active].load()
        self.set_hints(self.views[self._active].keyhints())
        self.trigger_async_refresh()
        self._alarm_handle = self.loop.set_alarm_in(10, self._schedule_refresh)
        self.loop.run()
        return self.result
