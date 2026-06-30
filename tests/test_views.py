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
        self._confirm_messages = []
        self._last_confirm = None
        self.footer_text = urwid.Text("")
        self.footer = urwid.AttrMap(self.footer_text, "footer")
        self.frame = urwid.Frame(urwid.Text("body"), footer=self.footer)
        self.views = []
        self._active = 0

    def notify(self, msg, seconds=3):
        self._notifications.append(msg)

    def confirm(self, message, on_yes):
        # Mirror App.confirm: record the prompt and capture the callback so a test
        # can simulate pressing `y` via `app._last_confirm()`.
        self._confirm_messages.append(message)
        self._last_confirm = on_yes

    def exit_with_resume(self, session, fork=False):
        self.result = ("resume", session, fork)

    def trigger_async_refresh(self):
        pass

    def set_hints(self, hints):
        self.footer_text.set_text(hints)

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
    # `h` moved into `?` help (D3 footer slim-down) — no longer in the footer hint.

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


def test_sessions_cleanup_mode(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod, "cleanup_classified", lambda *a, **k: {
        "empty": 10, "short": 5, "orphan_dirs": 3,
        "zombie_procs": 2, "aged_entries": 4,
    })
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._enter_cleanup()
    assert view._mode == "cleanup"
    # Five submenu actions now: empty/short/orphans/zombies/aged (CLI/TUI parity).
    assert len(view._cleanup_walker) == 5
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
    # The submenu counts (and the derived status-bar stats) now come from
    # cleanup_classified; stub it so the self-fetch path does no real disk IO.
    monkeypatch.setattr(sv_mod, "cleanup_classified", lambda *a, **k: {
        "empty": 0, "short": 0, "orphan_dirs": 0, "zombie_procs": 0, "aged_entries": 0,
    })
    monkeypatch.setattr(sv_mod.registry, "read_session_procs", lambda *a, **k: [])
    monkeypatch.setattr(sv_mod.registry, "read_agent_jobs", lambda *a, **k: [])
    monkeypatch.setattr(sv_mod.liveness, "alive_map", lambda *a, **k: {})

    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.fetch_pending()

    assert view._pending == fake
    assert view._cleanup_stats == {"total": 1, "empty": 0, "short": 0, "orphans": 0}
    assert view._pending_classified == {
        "empty": 0, "short": 0, "orphan_dirs": 0, "zombie_procs": 0, "aged_entries": 0,
    }


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
    assert "开机自启" in hints
    assert "自动远控" in hints
    assert "A/S" in hints  # batch keys are discoverable in the footer now


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


def test_rc_S_key_confirms_then_stops_all(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    stopped = {"n": 0}
    monkeypatch.setattr(rc_mod, "stop_all",
                        lambda: stopped.__setitem__("n", stopped["n"] + 1) or True)
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = [_make_project(name="p1", status="running")]
    view.apply_data()

    view.handle_key("S")
    assert stopped["n"] == 0  # nothing stopped until the confirm is accepted
    assert app._confirm_messages and "停止全部" in app._confirm_messages[0]

    app._last_confirm()  # simulate pressing y
    assert stopped["n"] == 1
    assert any("已停止全部" in m for m in app._notifications)


# === Unified-keys: Sessions terminate now `s` + confirms ====================

def test_sessions_s_key_confirms_then_terminates(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    killed = {"n": 0}
    monkeypatch.setattr(sv_mod, "terminate_session",
                        lambda s: killed.__setitem__("n", killed["n"] + 1) or True)
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", alive=True, current=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("s")
    assert killed["n"] == 0  # a confirm is requested, nothing killed yet
    assert app._confirm_messages and "停止" in app._confirm_messages[0]

    app._last_confirm()  # simulate pressing y
    assert killed["n"] == 1
    assert any("已停止" in m for m in app._notifications)


def test_sessions_s_key_guards_before_confirm(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod, "terminate_session", lambda s: True)
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("s")
    assert app._confirm_messages == []  # guard fires BEFORE any confirm
    assert any("未在运行" in m for m in app._notifications)


# === Unified confirm: takeover/relaunch live + degrade gate + f guard =======

def test_would_take_over_matches_resume_plan():
    from cc_session_control.actions.session_ops import _resume_plan, would_take_over
    live = _make_session(alive=True, current=False)
    dead = _make_session(alive=False)
    assert would_take_over(live) is _resume_plan(live)[2] is True
    assert would_take_over(dead) is _resume_plan(dead)[2] is False
    # fork is a copy — never a takeover.
    assert would_take_over(live, fork=True) is False


def test_sessions_enter_live_confirms_takeover(monkeypatch):
    import cc_session_control.views.sessions as sv_mod
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", alive=True, current=False, pid=999)]
    view._apply_filter(); view._rebuild()

    view.handle_key("enter")
    assert app.result is None  # not resumed until confirmed
    assert app._confirm_messages and "接回会话" in app._confirm_messages[0]
    assert "终止原进程" in app._confirm_messages[0]

    app._last_confirm()
    assert app.result is not None and app.result[0] == "resume"


def test_sessions_enter_dead_resumes_directly():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False, current=False)]
    view._apply_filter(); view._rebuild()

    view.handle_key("enter")
    assert app._confirm_messages == []  # dead: no takeover, no confirm
    assert app.result is not None and app.result[0] == "resume"


def test_sessions_R_live_confirms_relaunch(monkeypatch):
    import cc_session_control.views.sessions as sv_mod
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    relaunched = {"n": 0}
    monkeypatch.setattr(sv_mod, "relaunch_in_tmux",
                        lambda s: relaunched.__setitem__("n", relaunched["n"] + 1) or True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", alive=True, current=False)]
    view._apply_filter(); view._rebuild()

    view.handle_key("R")
    assert relaunched["n"] == 0
    assert app._confirm_messages and "转入后台" in app._confirm_messages[0]

    app._last_confirm()
    assert relaunched["n"] == 1
    assert any("已转入后台" in m for m in app._notifications)


def test_sessions_R_degraded_still_relaunches_dead(monkeypatch):
    # B3: relaunching a DEAD session kills nothing — must NOT be blocked off /proc.
    import cc_session_control.views.sessions as sv_mod
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    relaunched = {"n": 0}
    monkeypatch.setattr(sv_mod, "relaunch_in_tmux",
                        lambda s: relaunched.__setitem__("n", relaunched["n"] + 1) or True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False, current=False)]
    view._apply_filter(); view._rebuild()

    view.handle_key("R")
    assert relaunched["n"] == 1  # dead relaunch is not gated by degrade


def test_sessions_R_degraded_refuses_live_takeover(monkeypatch):
    import cc_session_control.views.sessions as sv_mod
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    relaunched = {"n": 0}
    monkeypatch.setattr(sv_mod, "relaunch_in_tmux",
                        lambda s: relaunched.__setitem__("n", relaunched["n"] + 1) or True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", alive=True, current=False)]
    view._apply_filter(); view._rebuild()

    view.handle_key("R")
    assert relaunched["n"] == 0
    assert app._confirm_messages == []
    assert any("降级" in m for m in app._notifications)


def test_sessions_f_refuses_current():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="cur", alive=True, current=True)]
    view._apply_filter(); view._rebuild()

    view.handle_key("f")
    assert app.result is None
    assert any("不能分叉当前会话" in m for m in app._notifications)


def test_rc_s_running_confirms_stop(monkeypatch):
    from cc_session_control.data import rc as rc_mod
    stopped = {"n": 0}
    monkeypatch.setattr(rc_mod, "stop_one",
                        lambda name: stopped.__setitem__("n", stopped["n"] + 1) or True)
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = [_make_project(name="p1", status="running")]
    view.apply_data()

    view.handle_key("s")
    assert stopped["n"] == 0
    assert app._confirm_messages and "停止远控服务" in app._confirm_messages[0]

    app._last_confirm()
    assert stopped["n"] == 1


def test_rc_s_not_running_no_confirm():
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = [_make_project(name="p1", status="stopped")]
    view.apply_data()

    view.handle_key("s")
    assert app._confirm_messages == []
    assert any("未在运行" in m for m in app._notifications)


# === Phase 7: D9 session badges + hide-filter union =========================

def test_session_row_renders_source_and_flag_badges():
    row = SessionRow(_make_session(
        source="cli", rc_exposed=True, agent_short="abcd1234"))
    text = _row_text(row)
    assert "CLI" in text       # source badge
    assert "📱" in text         # RC-exposure marker (phone; Emoji_Presentation, width-stable)
    # Agent-link is intentionally NOT a row marker anymore: orthogonal to 远控,
    # already covered by the 来源 BG badge + the 后台 tab. Lock that it is gone.
    assert "代" not in text
    assert "⚙" not in text


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
    # Classified is computed from the snapshot's own liveness inputs (no re-scan).
    seen = {}
    monkeypatch.setattr(sv_mod, "cleanup_classified",
                        lambda s, procs, cur, jobs, agents: seen.update(
                            n=len(s), procs=procs, cur=cur) or {
                            "empty": 0, "short": 0, "orphan_dirs": 0,
                            "zombie_procs": 0, "aged_entries": 0})

    fake = [_make_session(sid="snap1")]
    from cc_session_control.models import SessionProc
    snap = WorldSnapshot(sessions=fake,
                         session_procs=[SessionProc(pid=9, sid="snap1")],
                         cur={42})
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.fetch_pending(snap)
    assert view._pending == fake
    # Snapshot liveness inputs were projected straight through (no second scan).
    assert view._pending_procs == snap.session_procs
    assert view._pending_cur == {42}
    assert seen["cur"] == {42}


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


# === Post-review fix B: RC ledger honesty + tri-state ========================

def test_rc_view_env_section_shows_incomplete_caveat():
    # Fix 1 (red line #5): the env-ledger panel itself must carry the "inherently
    # incomplete" + manual-delete caveat, not only the `csctl env` CLI.
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._pending = []
    view._pending_servers = []
    view._pending_current = []
    view._pending_orphans = [BridgeEnv(prefix="env", key="O", status="orphan")]
    view.apply_data()
    blob = "\n".join(_row_text(view.walker[i]) for i in range(len(view.walker)))
    assert "不完整" in blob
    assert "未运行" in blob
    assert "云端需手动删除" in blob


def test_rc_view_help_warns_ledger_incomplete():
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._show_help()
    blob = "\n".join(_row_text(view.walker[i]) for i in range(len(view.walker)))
    assert "不完整" in blob
    assert "未运行" in blob
    assert "云端需手动删除" in blob


def test_rc_view_c_key_full_tristate_cycle(monkeypatch):
    # Fix 5: cycle must be None→True→False→None so explicit True is reachable.
    import cc_session_control.views.rc as rc_view_mod

    writes = []
    monkeypatch.setattr(rc_view_mod, "set_rc_at_startup",
                        lambda directory, value: writes.append(value))
    app = FakeApp()
    view = RCView(app)
    app.views = [view]

    for start, expected in ((None, True), (True, False), (False, None)):
        view._pending = [_make_project(name="p", rc_at_startup=start)]
        view.apply_data()
        view.handle_key("c")
        assert writes[-1] is expected


# === Post-review fix B: Sessions degraded honesty + cleanup parity ==========

def _focus_dead_session(view, **overrides):
    overrides.setdefault("alive", False)
    view._all_sessions = [_make_session(**overrides)]
    view._apply_filter()
    view._rebuild()
    view.walker.set_focus(0)


def test_delete_honest_feedback_true_then_false(monkeypatch):
    # Fix 3 / L4: only claim 已删除 when remove_session truly removed something.
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    _focus_dead_session(view)

    monkeypatch.setattr(sv_mod, "remove_session", lambda s: True)
    view.handle_key("d")
    assert app._notifications[-1] == "已删除"

    monkeypatch.setattr(sv_mod, "remove_session", lambda s: False)
    view.handle_key("d")
    assert app._notifications[-1] == "无可删除内容"


def test_delete_refuses_when_current_undeterminable(monkeypatch):
    # Fix 2a / R10: no /proc -> the delete must refuse honestly, not "delete".
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    removed = {"n": 0}
    monkeypatch.setattr(sv_mod, "remove_session",
                        lambda s: removed.__setitem__("n", removed["n"] + 1) or True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    _focus_dead_session(view)

    view.handle_key("d")
    assert removed["n"] == 0
    assert app._notifications[-1] == sv_mod._DEGRADED


def test_cleanup_preview_refuses_when_undeterminable_not_nothing(monkeypatch):
    # Fix 2a: a degraded refusal must NOT read as "无…需要清理".
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._enter_preview("empty")
    assert view._mode == "list"  # never opened a preview
    assert app._notifications[-1] == sv_mod._DEGRADED
    assert "需要清理" not in app._notifications[-1]


def test_cleanup_submenu_exposes_zombie_and_aged_actions(monkeypatch):
    # Fix 4: CLI/TUI parity — the submenu offers the pid-keyed zombie sweep and
    # the age sweep, with counts from cleanup_classified.
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod, "cleanup_classified", lambda *a, **k: {
        "empty": 1, "short": 2, "orphan_dirs": 3, "zombie_procs": 4, "aged_entries": 5,
    })
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._classified = sv_mod.cleanup_classified()
    view._enter_cleanup()
    keys = [w.action_key for w in view._cleanup_walker]
    assert keys == ["empty", "short", "orphans", "zombies", "aged"]
    blob = "\n".join(_row_text(w) for w in view._cleanup_walker)
    assert "4" in blob and "5" in blob  # zombie + aged counts surfaced


def test_zombie_sweep_preview_and_confirm(monkeypatch):
    # Fix 4: zombie sweep previews the dead pid files (from the shared snapshot's
    # session_procs/cur) and confirm routes to remove_zombie_session_files.
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.models import SessionProc

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._session_procs = [SessionProc(pid=111, sid="z", proc_alive=False)]
    view._cur = set()

    view._enter_preview("zombies")
    assert view._mode == "preview"
    assert view._preview_action == "zombies"

    swept = {"n": 0}
    monkeypatch.setattr(sv_mod, "remove_zombie_session_files",
                        lambda procs, cur: swept.__setitem__("n", len(procs)) or 1)
    view._confirm_cleanup()
    assert swept["n"] == 1
    assert any("僵尸会话文件" in m for m in app._notifications)


def test_zombie_sweep_gated_when_undeterminable(monkeypatch):
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.models import SessionProc

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._session_procs = [SessionProc(pid=111, sid="z", proc_alive=False)]
    view._cur = set()
    view._enter_preview("zombies")
    assert view._mode == "list"
    assert app._notifications[-1] == sv_mod._DEGRADED


def test_aged_sweep_preview_and_confirm_not_gated(monkeypatch):
    # Fix 4: the age sweep is mtime-only -> NOT R10-gated; works even with no /proc.
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    monkeypatch.setattr(sv_mod, "list_aged_entries", lambda *a, **k: ["shell-snapshots/old.sh"])
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]

    view._enter_preview("aged")
    assert view._mode == "preview"
    assert view._preview_action == "aged"

    swept = {"n": 0}
    monkeypatch.setattr(sv_mod, "remove_aged_entries",
                        lambda *a, **k: swept.__setitem__("n", 1) or 1)
    view._confirm_cleanup()
    assert swept["n"] == 1
    assert any("过期项" in m for m in app._notifications)
