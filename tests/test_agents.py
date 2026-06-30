"""AgentsView (后台 tab) unit tests — view logic only, no MainLoop/TTY."""

import urwid

import cc_session_control.views.agents as av_mod
from cc_session_control.actions import agent_ops
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import AgentJob
from cc_session_control.views.agents import AgentRow, AgentsView


class FakeApp:
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


def _make_job(**overrides):
    defaults = dict(
        short="abcdef01", sid="abcdef0123456789", resume_sid="abcdef0123456789",
        state="idle", tempo="fast", cwd="/tmp/proj", name="worker",
        env_suffix="XYZ", respawn_flags=[], host_pid=None, host_alive=False,
    )
    defaults.update(overrides)
    return AgentJob(**defaults)


def _make_view(jobs):
    app = FakeApp()
    view = AgentsView(app)
    app.views = [view]
    view._jobs = jobs
    view._rebuild()
    return app, view


# --- TabView protocol + basic widgets ---

def test_agents_view_satisfies_tabview_protocol():
    from cc_session_control.app import TabView
    assert isinstance(AgentsView(FakeApp()), TabView)


def test_agent_row_selectable_holds_job():
    job = _make_job()
    row = AgentRow(job)
    assert row.selectable()
    assert row.job.short == "abcdef01"


def test_agent_row_alive_marker():
    alive = AgentRow(_make_job(host_alive=True))
    text = b" ".join(alive.render((120,), focus=False).text).decode()
    assert "●" in text
    assert "worker" in text


# --- fetch_pending: snapshot projection vs self-fetch ---

def test_fetch_pending_uses_snapshot_agent_jobs():
    app = FakeApp()
    view = AgentsView(app)
    app.views = [view]
    jobs = [_make_job(short="s1")]
    snap = WorldSnapshot(agent_jobs=jobs)
    view.fetch_pending(snap)
    assert view._pending == jobs


def test_fetch_pending_self_fetch_enriches(monkeypatch):
    jobs = [_make_job(short="s2", host_alive=False)]
    monkeypatch.setattr(av_mod.registry, "read_agent_jobs", lambda *a, **k: jobs)
    monkeypatch.setattr(av_mod.agent_ops, "job_host", lambda job: (4242, True))

    app = FakeApp()
    view = AgentsView(app)
    app.views = [view]
    view.fetch_pending()  # no snapshot -> self fetch + enrich

    assert view._pending[0].host_pid == 4242
    assert view._pending[0].host_alive is True


def test_apply_data_swaps_pending_into_walker():
    app, view = _make_view([])
    view._pending = [_make_job(short="j1"), _make_job(short="j2")]
    view.apply_data()
    assert len(view.walker) == 2
    assert view._loaded is True


def test_load_enriches_and_renders(monkeypatch):
    monkeypatch.setattr(av_mod.registry, "read_agent_jobs", lambda *a, **k: [_make_job()])
    monkeypatch.setattr(av_mod.agent_ops, "job_host", lambda job: (None, False))
    app = FakeApp()
    view = AgentsView(app)
    app.views = [view]
    view.load()
    assert view._loaded is True
    assert len(view.walker) == 1


# --- keyhints reuse the agent_ops constants ---

def test_keyhints_reuse_agent_ops_constants():
    view = AgentsView(FakeApp())
    hints = view.keyhints()
    assert agent_ops.KEYHINTS in hints
    assert "帮助" in hints


# --- key dispatch: respawn / takeover / watch / remove / stop ---

def test_R_key_respawns(monkeypatch):
    # Unified verb table: respawn moved off `r` (now refresh) onto `R`.
    called = {}
    monkeypatch.setattr(av_mod.agent_ops, "respawn",
                        lambda job: called.setdefault("job", job) or "claude --resume x --bg")
    app, view = _make_view([_make_job()])
    view.handle_key("R")
    assert "job" in called
    assert any("已重启" in m for m in app._notifications)


def test_r_key_refreshes_not_respawn(monkeypatch):
    # `r` is refresh on EVERY tab now; it must NOT respawn.
    respawned = {"n": 0}
    monkeypatch.setattr(av_mod.agent_ops, "respawn",
                        lambda job: respawned.__setitem__("n", respawned["n"] + 1) or "x")
    app, view = _make_view([_make_job()])
    view.handle_key("r")
    assert respawned["n"] == 0
    assert any("刷新" in m for m in app._notifications)


def test_enter_key_takeover_like_o(monkeypatch):
    # Enter is the unified primary action; on this tab that is takeover (= `o`).
    monkeypatch.setattr(av_mod.agent_ops, "resume_takeover",
                        lambda job: _takeover_session(current=False))
    app, view = _make_view([_make_job()])
    view.handle_key("enter")
    assert app.result is not None
    assert app.result[0] == "resume"


def test_o_key_takeover_routes_to_exit_with_resume(monkeypatch):
    s = av_mod.agent_ops.resume_takeover  # keep ref
    monkeypatch.setattr(av_mod.agent_ops, "resume_takeover",
                        lambda job: _takeover_session(current=False))
    app, view = _make_view([_make_job()])
    view.handle_key("o")
    assert app.result is not None
    assert app.result[0] == "resume"


def test_o_key_takeover_refuses_current(monkeypatch):
    monkeypatch.setattr(av_mod.agent_ops, "resume_takeover",
                        lambda job: _takeover_session(current=True))
    app, view = _make_view([_make_job()])
    view.handle_key("o")
    assert app.result is None
    assert any("不能接回当前会话" in m for m in app._notifications)


def _takeover_session(current, alive=False):
    from cc_session_control.models import Session
    return Session(sid="x", cwd="/tmp", label="x", mtime=0.0, prompts=0,
                   pid=999 if alive else None, alive=alive, current=current, source="bg")


def test_o_key_live_worker_confirms_takeover(monkeypatch):
    # B1: takeover of a RUNNING worker kills its host pid → must confirm first.
    monkeypatch.setattr(av_mod.agent_ops, "resume_takeover",
                        lambda job: _takeover_session(current=False, alive=True))
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("o")
    assert app.result is None  # not resumed yet
    assert app._confirm_messages and "接回后台 agent" in app._confirm_messages[0]
    assert "终止原进程" in app._confirm_messages[0]
    app._last_confirm()  # simulate pressing y
    assert app.result is not None and app.result[0] == "resume"


def test_o_key_dead_worker_takes_over_directly(monkeypatch):
    monkeypatch.setattr(av_mod.agent_ops, "resume_takeover",
                        lambda job: _takeover_session(current=False, alive=False))
    app, view = _make_view([_make_job(host_alive=False)])
    view.handle_key("o")
    assert app._confirm_messages == []  # dead worker: no takeover, no confirm
    assert app.result is not None and app.result[0] == "resume"


def test_d_key_refuses_live_job(monkeypatch):
    removed = {"n": 0}
    monkeypatch.setattr(av_mod.agent_ops, "remove_job",
                        lambda job: removed.__setitem__("n", removed["n"] + 1) or True)
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("d")
    assert removed["n"] == 0
    assert any("运行中的后台 agent 不能删除" in m for m in app._notifications)


def test_d_key_removes_settled_job(monkeypatch):
    monkeypatch.setattr(av_mod.agent_ops, "remove_job", lambda job: True)
    app, view = _make_view([_make_job(host_alive=False)])
    view.handle_key("d")
    assert any("已删除" in m for m in app._notifications)


def test_s_key_stops_live_with_orphan_warning(monkeypatch):
    # Unified confirm: `s` on a live worker confirms first, then `_last_confirm()`
    # runs the stop body whose notify carries the orphan-risk warning.
    monkeypatch.setattr(av_mod.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(av_mod.agent_ops, "stop_job", lambda job: True)
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("s")
    assert app._confirm_messages  # a confirm is requested first
    app._last_confirm()           # simulate pressing y
    assert any("孤儿" in m for m in app._notifications)


def test_s_key_refuses_dead_worker(monkeypatch):
    monkeypatch.setattr(av_mod.proc, "current_determinable", lambda: True)
    stopped = {"n": 0}
    monkeypatch.setattr(av_mod.agent_ops, "stop_job",
                        lambda job: stopped.__setitem__("n", stopped["n"] + 1) or True)
    app, view = _make_view([_make_job(host_alive=False)])
    view.handle_key("s")
    assert stopped["n"] == 0
    assert app._confirm_messages == []  # guard fires before any confirm
    assert any("后台 agent 未在运行" in m for m in app._notifications)


def test_help_mode_and_return():
    app, view = _make_view([_make_job()])
    view.handle_key("?")
    assert view._mode == "help"
    assert view.keyhints() == "按任意键返回"
    view.handle_key("x")  # any key returns
    assert view._mode == "list"
