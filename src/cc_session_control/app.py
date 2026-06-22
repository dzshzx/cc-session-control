"""CCM — Claude Code Manager urwid App."""

from __future__ import annotations

import curses
import os
import threading

import urwid

from .data.rc import scan as rc_scan
from .data.sessions import cleanup_stats, scan as sessions_scan
from .views.cleanup import CleanupView
from .views.rc import RCView
from .views.sessions import SessionsView

# 6-tuple: (name, fg_16, bg_16, mono, fg_256, bg_256)
PALETTE = [
    ("header",     "white,bold",  "black", "bold",     "#fff,bold",  "#111"),
    ("footer",     "light gray",  "black", None,       "#999",       "#111"),
    ("tab_on",     "white,bold",  "black", "bold,underline", "#fff,bold,underline", "#111"),
    ("tab_off",    "dark cyan",   "black", None,       "#688",       "#111"),
    ("alive",      "light green", "black", None,       "#6d6",       "#111"),
    ("dead",       "light gray",  "black", None,       "#ccc",       "#111"),
    ("selected",   "white,bold",  "dark cyan", "standout", "#fff,bold", "#068"),
    ("notify",     "yellow,bold", "black", "bold",     "#ff0,bold",  "#111"),
    ("status",     "light gray",  "black", None,       "#aaa",       "#111"),
    ("rc_running", "light green", "black", None,       "#6d6",       "#111"),
    ("rc_stopped", "light gray",  "black", None,       "#ccc",       "#111"),
    ("body",       "light gray",  "black", None,       "#ccc",       "#111"),
]

TAB_NAMES = ["会话", "远程控制", "清理"]


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

        self.views = [SessionsView(self), RCView(self), CleanupView(self)]
        self._active = 0

        self.body = urwid.WidgetPlaceholder(self.views[0].widget)
        self._tab_texts: list[urwid.Text] = []
        tab_bar = self._build_tab_bar()
        title = urwid.AttrMap(urwid.Text(" Claude Code 管理器", align="left"), "header")
        self.header = urwid.Pile([title, tab_bar])

        self._footer_default = " Tab 切换 · q 退出 · r 刷新"
        self.footer_text = urwid.Text(self._footer_default)
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
        hints = self.views[self._active].keyhints()
        self.footer_text.set_text(f" Tab 切换 · q 退出 · {hints}")
        if not self.views[self._active]._loaded:
            self.trigger_async_refresh()

    def _input(self, key: str) -> None:
        if key == "tab":
            self._switch_tab()
        elif key == "q":
            self._exit()
        else:
            self.views[self._active].handle_key(key)

    def _exit(self, result: tuple | None = None) -> None:
        self._exiting = True
        if self._alarm_handle:
            self.loop.remove_alarm(self._alarm_handle)
        self.result = result
        raise urwid.ExitMainLoop()

    def exit_with_resume(self, session: object, fork: bool = False) -> None:
        self._exit(("resume", session, fork))

    def notify(self, msg: str, seconds: float = 3) -> None:
        self.frame.footer = urwid.AttrMap(urwid.Text(f" {msg}"), "notify")
        self.loop.set_alarm_in(seconds, lambda *_: self._restore_footer())

    def _restore_footer(self) -> None:
        self.frame.footer = self.footer

    def trigger_async_refresh(self) -> None:
        if self._refreshing or self._exiting:
            return
        self._refreshing = True

        def worker() -> None:
            try:
                sessions = sessions_scan()
                stats = cleanup_stats(sessions)
                rc_projects = rc_scan()
                sv, rv, cv = self.views
                sv.set_pending(sessions)
                rv.set_pending(rc_projects)
                cv.set_pending_stats(stats)
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
        hints = self.views[self._active].keyhints()
        self.footer_text.set_text(f" Tab 切换 · q 退出 · {hints}")
        self.trigger_async_refresh()
        self._alarm_handle = self.loop.set_alarm_in(10, self._schedule_refresh)
        self.loop.run()
        return self.result
