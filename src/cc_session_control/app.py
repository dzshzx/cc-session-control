"""CCM — Claude Code Manager urwid App."""

from __future__ import annotations

import os
import threading

import urwid

from .views.cleanup import CleanupView
from .views.rc import RCView
from .views.sessions import SessionsView

PALETTE = [
    ("header", "white,bold", "default"),
    ("footer", "dark gray", "default"),
    ("tab_on", "white,bold,underline", "default"),
    ("tab_off", "dark gray", "default"),
    ("alive", "light green", "default"),
    ("dead", "white", "default"),
    ("selected", "standout", "default"),
    ("notify", "yellow", "default"),
    ("status", "dark gray", "default"),
    ("rc_running", "light green", "default"),
    ("rc_stopped", "white", "default"),
]

TAB_NAMES = ["会话", "远程控制", "清理"]


class App:
    def __init__(self) -> None:
        self.result: tuple | None = None
        self._exiting = False
        self._alarm_handle: object | None = None
        self._pipe_fd: int | None = None

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
        self.loop = urwid.MainLoop(
            self.frame,
            palette=PALETTE,
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

    def _schedule_refresh(self, loop: object = None, data: object = None) -> None:
        if self._exiting:
            return
        view = self.views[self._active]

        def worker() -> None:
            view.refresh_data()
            if self._pipe_fd is not None:
                try:
                    os.write(self._pipe_fd, b"1")
                except OSError:
                    pass

        threading.Thread(target=worker, daemon=True).start()
        self._alarm_handle = self.loop.set_alarm_in(10, self._schedule_refresh)

    def _on_pipe(self, data: bytes) -> bool:
        if not self._exiting:
            self.views[self._active].apply_data()
        return True

    def run(self) -> tuple | None:
        self._pipe_fd = self.loop.watch_pipe(self._on_pipe)
        self.views[self._active].load()
        self._alarm_handle = self.loop.set_alarm_in(10, self._schedule_refresh)
        self.loop.run()
        return self.result
