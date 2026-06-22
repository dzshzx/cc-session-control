"""Sessions view — urwid ListBox with keyboard actions."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import urwid

from ..actions.session_ops import resume_cmd, terminate_session, to_clipboard
from ..data.agents import invalidate_cache
from ..data.sessions import remove_session
from ..models import Session

if TYPE_CHECKING:
    from ..app import App


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


class SessionsView:
    def __init__(self, app: App) -> None:
        self.app = app
        self._sessions: list[Session] = []
        self._all_sessions: list[Session] = []
        self._pending: list[Session] | None = None
        self._loaded = False
        self._filtering = False
        self._filter_text = ""

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        body = urwid.AttrMap(self.listbox, {None: "body"})
        self.widget = urwid.Frame(body, header=self.status)

    def keyhints(self) -> str:
        return "Enter 接回 · f 分叉 · t 终止 · d 删除 · y 复制 · / 过滤"

    def load(self) -> None:
        from ..data.sessions import scan
        sessions = scan()
        self._all_sessions = sessions
        self._sessions = sessions
        self._loaded = True
        self._rebuild()

    def set_pending(self, sessions: list[Session]) -> None:
        self._pending = sessions

    def apply_data(self) -> None:
        if self._pending is not None:
            self._all_sessions = self._pending
            self._pending = None
            self._loaded = True
            self._apply_filter()
            self._rebuild()

    def _rebuild(self) -> None:
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        for s in self._sessions:
            self.walker.append(SessionRow(s))
        if self.walker and focus_pos is not None:
            self.walker.set_focus(min(focus_pos, len(self.walker) - 1))
        alive_n = sum(1 for s in self._all_sessions if s.alive)
        flt = f" · 过滤「{self._filter_text}」" if self._filter_text else ""
        self.status.original_widget.set_text(f" 共 {len(self._all_sessions)} 条会话 · 活 {alive_n} · 显示 {len(self._sessions)}{flt}")

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
        self._filtering = True
        self._filter_edit = urwid.Edit("过滤: ")
        self.app.frame.footer = urwid.AttrMap(self._filter_edit, "notify")

    def _exit_filter(self, cancel: bool = False) -> None:
        self._filtering = False
        if cancel:
            self._filter_text = ""
        else:
            self._filter_text = self._filter_edit.get_edit_text()
        self._apply_filter()
        self._rebuild()
        self.app._restore_footer()

    def handle_key(self, key: str) -> None:
        if self._filtering:
            if key == "enter":
                self._exit_filter()
            elif key == "esc":
                self._exit_filter(cancel=True)
            else:
                self.widget.keypress((80,), key)
            return

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
        elif key == "r":
            self.app.trigger_async_refresh()
            self.app.notify("刷新中…")
        elif key == "/":
            self._enter_filter()
