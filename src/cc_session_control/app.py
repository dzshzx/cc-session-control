"""CCM — Claude Code Manager Textual App."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .views.cleanup import CleanupView
from .views.rc import RCView
from .views.sessions import SessionsView


class CCMApp(App):
    TITLE = "Claude Code Manager"
    CSS = """
    Screen {
        layout: vertical;
    }
    TabbedContent {
        height: 1fr;
    }
    #sessions-table, #rc-table {
        height: 1fr;
    }
    #sessions-status, #rc-status {
        height: 1;
        padding: 0 1;
        background: $surface;
    }
    #cleanup-stats {
        padding: 1 2;
    }
    #cleanup-result {
        padding: 0 2;
        color: $success;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Sessions", id="tab-sessions"):
                yield SessionsView()
            with TabPane("Remote Ctrl", id="tab-rc"):
                yield RCView()
            with TabPane("Cleanup", id="tab-cleanup"):
                yield CleanupView()
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(10.0, self._auto_refresh)

    def _auto_refresh(self) -> None:
        active = self.query_one(TabbedContent).active
        if active == "tab-sessions":
            self.query_one(SessionsView).load_data()
        elif active == "tab-rc":
            self.query_one(RCView).load_data()
