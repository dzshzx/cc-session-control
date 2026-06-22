"""View unit tests — construct widgets and verify basic behavior without MainLoop."""

import urwid

from cc_session_control.models import RCProject, Session
from cc_session_control.views.sessions import SessionRow, SessionsView
from cc_session_control.views.rc import RCRow, RCView


class FakeApp:
    """Minimal stub for App used by views."""
    def __init__(self):
        self.result = None
        self._notifications = []
        self.footer_text = urwid.Text("")
        self.views = []
        self._active = 0

    def notify(self, msg, seconds=3):
        self._notifications.append(msg)

    def exit_with_resume(self, session, fork=False):
        self.result = ("resume", session, fork)


def _make_session(**overrides):
    defaults = dict(sid="abc123", cwd="/tmp/proj", label="test session",
                    mtime=1700000000.0, prompts=5, pid=None, alive=False,
                    current=False, hidden=set(), file="/tmp/abc123.jsonl")
    defaults.update(overrides)
    return Session(**defaults)


def _make_project(**overrides):
    defaults = dict(name="myproj", directory="/tmp/myproj", trusted=True,
                    in_list=True, status="stopped", auto_start=True)
    defaults.update(overrides)
    return RCProject(**defaults)


def test_session_row_selectable():
    s = _make_session()
    row = SessionRow(s)
    assert row.selectable()
    assert row.session.sid == "abc123"


def test_session_row_alive_vs_dead():
    alive = SessionRow(_make_session(alive=True, pid=1234))
    dead = SessionRow(_make_session(alive=False))
    assert alive.session.alive
    assert not dead.session.alive


def test_sessions_view_construct():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    assert view.widget is not None
    assert len(view.walker) == 0


def test_sessions_view_filter_logic():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="a1", label="deploy fix"),
        _make_session(sid="a2", label="config change"),
        _make_session(sid="a3", label="deploy rollback"),
    ]
    view._filter_text = "deploy"
    view._apply_filter()
    assert len(view._sessions) == 2
    view._filter_text = ""
    view._apply_filter()
    assert len(view._sessions) == 3


def test_sessions_cleanup_mode():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._cleanup_stats = {"total": 100, "empty": 10, "short": 5, "orphans": 3}
    view._enter_cleanup()
    assert view._mode == "cleanup"
    assert len(view._cleanup_walker) == 3
    view._exit_cleanup()
    assert view._mode == "list"


def test_rc_row_selectable():
    p = _make_project()
    row = RCRow(p)
    assert row.selectable()
    assert row.project.name == "myproj"


def test_rc_view_construct():
    app = FakeApp()
    view = RCView(app)
    assert view.widget is not None


def test_sessions_view_fetch_pending(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    fake = [_make_session(sid="x1")]
    monkeypatch.setattr(sv_mod, "scan", lambda: fake)
    monkeypatch.setattr(sv_mod, "cleanup_stats",
                        lambda s: {"total": 1, "empty": 0, "short": 0, "orphans": 0})

    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.fetch_pending()

    assert view._pending == fake
    assert view._cleanup_stats == {"total": 1, "empty": 0, "short": 0, "orphans": 0}


def test_rc_view_fetch_pending(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    fake = [_make_project(name="p1")]
    monkeypatch.setattr(rc_mod, "scan", lambda: fake)

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view.fetch_pending()

    assert view._pending == fake
