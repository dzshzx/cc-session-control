"""View unit tests — construct widgets and verify basic behavior without MainLoop."""

import ast
import os

import urwid

from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import BridgeEnv, RCProject, RCServer, Session
from cc_session_control.views.sessions import SessionRow, SessionsView
from cc_session_control.views.rc import EnvRow, RCRow, RCView, ServerRow


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


# === Phase 7: D9 session badges + hide-filter union =========================

def test_session_row_renders_source_and_flag_badges():
    row = SessionRow(_make_session(
        source="cli", rc_exposed=True, agent_short="abcd1234"))
    text = _row_text(row)
    assert "CLI" in text       # source badge
    assert "📱" in text         # exposure marker
    assert "⚙" in text          # agent-link marker


def test_session_row_source_badge_maps_vscode_to_ide():
    text = _row_text(SessionRow(_make_session(source="vscode")))
    assert "IDE" in text


def test_hide_filter_unions_source_sdk(monkeypatch):
    # A session flagged sdk via the REGISTRY source (not a transcript `hidden`
    # tag) must still be hidden by the `h` toggle (D9 union via bridge_or_sdk).
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", source="cli", hidden=set()),
        _make_session(sid="sdkreg", source="sdk", hidden=set()),
    ]
    view._apply_filter()
    assert [s.sid for s in view._sessions] == ["normal", "sdkreg"]

    view._show_hidden = False
    view._apply_filter()
    assert [s.sid for s in view._sessions] == ["normal"]


def test_sessions_view_fetch_pending_uses_snapshot(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod, "scan", lambda: (_ for _ in ()).throw(
        AssertionError("scan() must not run when a snapshot is provided")))
    monkeypatch.setattr(sv_mod, "cleanup_stats",
                        lambda s: {"total": len(s), "empty": 0, "short": 0, "orphans": 0})

    fake = [_make_session(sid="snap1")]
    snap = WorldSnapshot(sessions=fake)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.fetch_pending(snap)
    assert view._pending == fake


# === Phase 7: RC tri-state + spawn_mode + servers + env ledger ==============

def test_rc_row_rc_at_startup_tristate():
    assert "未设置" in _row_text(RCRow(_make_project(rc_at_startup=None)))
    assert "开" in _row_text(RCRow(_make_project(rc_at_startup=True)))
    assert "关" in _row_text(RCRow(_make_project(rc_at_startup=False)))


def test_rc_row_shows_spawn_mode():
    assert "same-dir" in _row_text(RCRow(_make_project(spawn_mode="same-dir")))


def test_server_row_managed_external_badge():
    managed = _row_text(ServerRow(RCServer(name="ws/a", managed=True, pid=1, status="running")))
    external = _row_text(ServerRow(RCServer(name="ws/b", managed=False, pid=2, status="running")))
    assert "托管" in managed
    assert "外部" in external


def test_env_row_orphan_shows_manual_delete_literal():
    text = _row_text(EnvRow(BridgeEnv(prefix="env", key="ABC", status="orphan")))
    assert "云端需手动删除" in text
    assert "env_ABC" in text


def test_rc_view_renders_env_ledger_sections(monkeypatch):
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = [_make_project(name="p1")]
    view._pending_servers = [RCServer(name="ws/ext", managed=False, pid=7, status="running")]
    view._pending_current = [BridgeEnv(prefix="cse", key="AAA", bound_sid="sid-a", status="current")]
    view._pending_orphans = [BridgeEnv(prefix="env", key="ORPH", status="orphan")]
    view.apply_data()

    texts = [_row_text(view.walker[i]) for i in range(len(view.walker))]
    blob = "\n".join(texts)
    assert any("云端需手动删除" in t for t in texts)  # manual-delete divider/row
    assert "env_ORPH" in blob                          # orphan listed
    assert "cse_AAA" in blob                            # current listed
    assert "外部" in blob                               # external server badge


def test_rc_view_fetch_pending_uses_snapshot(monkeypatch):
    import cc_session_control.views.rc as rc_view_mod

    monkeypatch.setattr(rc_view_mod.environments, "current_envs",
                        lambda obs: [BridgeEnv(prefix="cse", key="C", status="current")])
    monkeypatch.setattr(rc_view_mod.environments, "orphan_envs",
                        lambda obs: [BridgeEnv(prefix="env", key="O", status="orphan")])

    snap = WorldSnapshot(
        rc_projects=[_make_project(name="p1")],
        rc_servers=[RCServer(name="ws/x", managed=True, pid=3, status="running")],
        observed_envs=[],
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view.fetch_pending(snap)

    assert view._pending[0].name == "p1"
    assert view._pending_servers[0].name == "ws/x"
    assert [e.env_id for e in view._pending_current] == ["cse_C"]
    assert [e.env_id for e in view._pending_orphans] == ["env_O"]


def test_rc_view_server_and_env_rows_are_read_only(monkeypatch):
    # External servers / env rows must NOT be actionable (no takeover/restart/
    # deregister key). Focusing such a row makes every key a no-op (AC9 red line).
    import cc_session_control.views.rc as rc_view_mod

    started = {"n": 0}
    monkeypatch.setattr(rc_view_mod.rc, "start_one",
                        lambda name: started.__setitem__("n", started["n"] + 1) or True)
    monkeypatch.setattr(rc_view_mod.rc, "stop_one",
                        lambda name: started.__setitem__("n", started["n"] + 1) or True)

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._projects = []
    view._servers = [RCServer(name="ws/ext", managed=False, pid=9, status="running")]
    view._orphans = [BridgeEnv(prefix="env", key="O", status="orphan")]
    view._rebuild()

    # Focus the external ServerRow explicitly.
    for i in range(len(view.walker)):
        if isinstance(view.walker[i], ServerRow):
            view.walker.set_focus(i)
            break
    assert view._selected() is None  # not an RCProject -> nothing actionable

    for key in ("enter", "s", "a", "c"):
        view.handle_key(key)
    assert started["n"] == 0
    assert app._notifications == []


# === AC9: red-line grep/AST assertions ======================================

_SRC_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src", "cc_session_control",
)


def _iter_src_files():
    for root, _dirs, files in os.walk(_SRC_ROOT):
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_no_deregister_or_delete_env_symbols_in_src():
    # AC9: no SYMBOL named deregister/delete_env may be defined, assigned, or
    # called anywhere in src (docstring prose mentioning the word is fine — this
    # walks the AST, not the text).
    forbidden = {"deregister", "delete_env"}
    offenders = []
    for path in _iter_src_files():
        with open(path) as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden:
                offenders.append((path, node.name))
            elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append((path, node.attr))
            elif isinstance(node, ast.Name) and node.id in forbidden:
                offenders.append((path, node.id))
    assert offenders == []


def test_environments_and_agent_ops_do_not_export_deregister():
    from cc_session_control.actions import agent_ops
    from cc_session_control.data import environments

    for mod in (environments, agent_ops):
        assert not hasattr(mod, "deregister")
        assert not hasattr(mod, "delete_env")


def test_rc_view_source_carries_manual_delete_literal():
    import cc_session_control.views.rc as rc_view_mod

    with open(rc_view_mod.__file__) as fh:
        assert "云端需手动删除" in fh.read()
