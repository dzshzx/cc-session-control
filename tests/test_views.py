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
        self.footer = urwid.AttrMap(self.footer_text, "footer")
        self.frame = urwid.Frame(urwid.Text("body"), footer=self.footer)
        self.views = []
        self._active = 0

    def notify(self, msg, seconds=3):
        self._notifications.append(msg)

    def exit_with_resume(self, session, fork=False):
        self.result = ("resume", session, fork)

    def trigger_async_refresh(self):
        pass

    def _restore_footer(self):
        self.frame.footer = self.footer


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


def _row_text(row):
    canvas = row.render((120,), focus=False)
    return b"\n".join(canvas.text).decode()


def test_views_satisfy_tabview_protocol():
    from cc_session_control.app import TabView
    assert isinstance(SessionsView(FakeApp()), TabView)
    assert isinstance(RCView(FakeApp()), TabView)


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


def test_session_row_labels_hidden_bridge_and_sdk_sessions():
    row = SessionRow(_make_session(label="phone session", hidden={"bridge", "sdk"}))
    text = _row_text(row)
    assert "[桥接 SDK] phone session" in text


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


def test_sessions_view_shows_hidden_sessions_by_default():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", hidden=set()),
        _make_session(sid="bridge", hidden={"bridge"}),
    ]

    view._apply_filter()

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]


def test_sessions_view_h_key_toggles_hidden_sessions():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", hidden=set()),
        _make_session(sid="bridge", hidden={"bridge"}),
    ]
    view._apply_filter()
    view._rebuild()

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]
    assert "桥接/SDK 1" in view.status.original_widget.get_text()[0]

    view.handle_key("h")

    assert [s.sid for s in view._sessions] == ["normal"]
    assert "桥接/SDK已隐藏 1" in view.status.original_widget.get_text()[0]
    assert "h 显示桥接项" in app.footer_text.get_text()[0]

    view.handle_key("h")

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]


def test_sessions_view_filter_respects_hidden_toggle():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", label="plain deploy", hidden=set()),
        _make_session(sid="bridge", label="mobile deploy", hidden={"bridge"}),
    ]
    view._filter_text = "deploy"

    view._apply_filter()

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]

    view._show_hidden = False
    view._apply_filter()

    assert [s.sid for s in view._sessions] == ["normal"]


def test_sessions_view_filter_mode_routes_text_to_edit():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]

    view.handle_key("/")
    view.handle_key("d")

    assert view._filter_edit.get_edit_text() == "d"


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


def test_sessions_short_cleanup_preview_excludes_empty_sessions(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    sessions = [
        _make_session(sid="empty", prompts=0),
        _make_session(sid="short1", prompts=1),
        _make_session(sid="short2", prompts=2),
        _make_session(sid="long", prompts=3),
    ]
    monkeypatch.setattr(sv_mod, "scan", lambda: sessions)

    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]

    view._enter_preview("short")

    assert {s.sid for s in view._preview_sessions} == {"short1", "short2"}


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


def test_rc_view_keyhints_uses_new_labels():
    view = RCView(FakeApp())
    hints = view.keyhints()
    assert "切换开机自启" in hints
    assert "切换自动远控" in hints


def test_rc_view_status_bar_counts_use_new_labels():
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = [
        _make_project(name="p1", auto_start=True, rc_at_startup=None),
        _make_project(name="p2", auto_start=True, rc_at_startup=False),
        _make_project(name="p3", auto_start=False, rc_at_startup=False),
    ]
    view.apply_data()
    text = view.status.original_widget.get_text()[0]
    assert "开机自启 2" in text
    assert "自动远控关 2" in text


def test_rc_view_c_key_notifies_with_new_label(monkeypatch):
    import cc_session_control.views.rc as rc_view_mod

    writes = []
    monkeypatch.setattr(rc_view_mod, "set_rc_at_startup",
                        lambda directory, value: writes.append((directory, value)))

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = [_make_project(name="p1", rc_at_startup=None)]
    view.apply_data()

    view.handle_key("c")

    assert writes  # toggle routed through the seam, not real disk
    assert any("自动远控" in m for m in app._notifications)


def test_rc_view_a_key_notifies_with_new_label(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    monkeypatch.setattr(rc_mod, "toggle_autostart", lambda name: True)

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = [_make_project(name="p1")]
    view.apply_data()

    view.handle_key("a")

    assert any("开机自启" in m for m in app._notifications)
