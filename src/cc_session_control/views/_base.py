"""Shared scaffolding for the list tabs — the concrete half of `TabView`.

The `TabView` Protocol in `app.py` is the *interface* App drives; this base
class is the shared *implementation* behind it: the walker/listbox/status
frame, the focus-preserving rebuild, the centered overlay, the footer-hints
guard, and the overlay-mode key dispatch. Subclasses supply rows and key
semantics (`_build_rows`, `_status_text`, `handle_key`, `keyhints`, their own
modes) and never re-inline this plumbing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

if TYPE_CHECKING:
    from ..app import App


class ListTabView:
    """Walker/listbox/overlay plumbing shared by the 会话/项目/后台 tabs."""

    # Overlay width in relative % — Sessions narrows to 70 for its previews.
    OVERLAY_WIDTH = 80

    def __init__(self, app: App, header: urwid.Widget) -> None:
        self.app = app
        self._loaded = False
        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        col_header = urwid.AttrMap(header, "col_header")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        self._list_body = urwid.AttrMap(self.listbox, {None: "body"})
        self._body = urwid.WidgetPlaceholder(self._list_body)
        self.widget = urwid.Frame(self._body, header=col_header, footer=self.status)

    # --- TabView default hooks ---

    def deactivate(self) -> None:
        """TabView hook: called on tab switch-away. Body-widget overlays stay
        (they remain visibly modal); views with transient FOOTER modes (e.g.
        the Sessions filter Edit) override this."""

    # --- rendering plumbing ---

    def _rebuild(self) -> None:
        """Rebuild the list walker in place, preserving the focused position."""
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        self._build_rows()
        if self.walker and focus_pos is not None:
            self.walker.set_focus(min(focus_pos, len(self.walker) - 1))
        self.status.original_widget.set_text(self._status_text())

    def _build_rows(self) -> None:
        """Append the tab's rows (incl. any empty placeholder) to the walker."""
        raise NotImplementedError

    def _status_text(self) -> str:
        """The status-bar line rendered after every rebuild."""
        raise NotImplementedError

    def _focused_widget(self) -> urwid.Widget | None:
        if not self.walker:
            return None
        return self.walker.get_focus()[0]

    # --- key-table dispatch (see views/_keytable.py) ---

    #: the view's single-source key table; subclasses override.
    KEY_TABLE: tuple = ()

    def _dispatch_key(self, key: str) -> None:
        """List-mode dispatch driven by `KEY_TABLE` — the same declaration that
        generates the footer hints and the help overlay. A selection-needing
        key with nothing selected is ignored (matches the old `and s` guards)."""
        for e in self.KEY_TABLE:
            if key not in e.keys:
                continue
            handler = getattr(self, e.handler)
            if e.needs_selection:
                sel = self._selected()
                if sel is not None:
                    handler(sel)
            else:
                handler()
            return

    def _key_refresh(self) -> None:
        """`r` — the footer-prefix promise, dispatched like any table key."""
        self.app.refresh_with_notice()

    def _update_footer(self) -> None:
        if self.app.is_active(self):
            self.app.set_hints(self.keyhints())

    def _show_overlay(
        self, title: str, rows: list, height: int | None = None
    ) -> None:
        walker = urwid.SimpleFocusListWalker(rows)
        listbox = urwid.ListBox(walker)
        header = urwid.AttrMap(urwid.Text(f" {title}", align="center"), "col_header")
        box = urwid.LineBox(urwid.Frame(listbox, header=header))
        h = height or min(len(rows) + 4, 30)
        self._body.original_widget = urwid.Overlay(
            box, self._list_body,
            align="center", width=("relative", self.OVERLAY_WIDTH),
            valign="middle", height=h,
        )

    # --- overlay-mode dispatch ---

    def _handle_overlay_key(self, key: str) -> None:
        """Overlay-mode keys: `r` keeps its footer-prefix meaning (刷新) so the
        "其余任意键返回" hint stays exact; any other key closes the overlay."""
        if key == "r":
            self.app.refresh_with_notice()
            return
        self._exit_overlay()

    def _exit_overlay(self) -> None:
        self._close_overlay_mode()
        self._body.original_widget = self._list_body
        self._rebuild()
        self._update_footer()

    def _close_overlay_mode(self) -> None:
        """Reset the subclass's overlay-mode flag back to its list mode."""
        raise NotImplementedError
