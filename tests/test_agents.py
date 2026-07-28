"""AgentsView (后台 tab) unit tests — view logic only, no MainLoop/TTY."""

import urwid

import cc_session_control.views.agents as av_mod
from cc_session_control.actions.runner import Accepted
from cc_session_control.actions.session_ops import (
    AttachIntent,
    ResumeIntent,
    TmuxResumeIntent,
)
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.refresh import RefreshBatch
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import AgentJob
from cc_session_control.views.agents import AgentRow, AgentsView


class FakeApp:
    def __init__(self):
        self.result = None
        self._notifications = []
        self._confirm_messages = []
        self._last_confirm = None
        self._submitted_actions = []
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

    def exit_with(self, intent):
        self.result = intent

    def trigger_async_refresh(self):
        pass

    def submit_action(self, action_key, action):
        self._submitted_actions.append(action_key)
        result = action()
        self.notify(result.message)
        if result.needs_refresh:
            self.trigger_async_refresh()
        return Accepted(action_key)

    def refresh_with_notice(self):
        self.trigger_async_refresh()
        self.notify("刷新中…")

    def set_hints(self, hints):
        self.footer_text.set_text(hints)

    def _restore_footer(self):
        self.frame.footer = self.footer

    def is_active(self, view):
        return not self.views or self.views[self._active] is view


def _make_job(**overrides):
    defaults = dict(
        short="abcdef01",
        sid="abcdef0123456789",
        resume_sid="abcdef0123456789",
        state="idle",
        tempo="fast",
        cwd="/tmp/proj",
        name="worker",
        env_suffix="XYZ",
        respawn_flags=[],
        host_pid=None,
        host_alive=False,
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


def _refresh_batch(jobs):
    snapshot = WorldSnapshot(agent_jobs=jobs)
    return RefreshBatch(
        generation=1,
        snapshot=snapshot,
        cleanup_plan=CleanupPlan(),
        cleanup_counts={},
        session_stats={},
        ordered_projects=(),
    )


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


# --- atomic refresh application ---


def test_apply_refresh_uses_snapshot_agent_jobs():
    app = FakeApp()
    view = AgentsView(app)
    app.views = [view]
    jobs = [_make_job(short="s1")]
    view.apply_refresh(_refresh_batch(jobs))
    assert view._jobs == jobs


def test_apply_refresh_rebuilds_walker():
    app, view = _make_view([])
    view.apply_refresh(_refresh_batch([_make_job(short="j1"), _make_job(short="j2")]))
    assert len(view.walker) == 2
    assert view._loaded is True


def test_apply_refresh_renders_one_job():
    app = FakeApp()
    view = AgentsView(app)
    app.views = [view]
    view.apply_refresh(_refresh_batch([_make_job()]))
    assert view._loaded is True
    assert len(view.walker) == 1


# --- keyhints are generated from the view's KEY_TABLE ---


def test_keyhints_generated_from_key_table():
    view = AgentsView(FakeApp())
    hints = view.keyhints()
    assert "Enter 接回" in hints
    assert "t 终端接回" in hints
    assert "详细说明" in hints


# --- key dispatch: respawn / takeover / watch / remove / stop ---


def test_R_key_respawns(monkeypatch):
    # Unified verb table: respawn moved off `r` (now refresh) onto `R`.
    called = {}
    monkeypatch.setattr(
        av_mod.agent_ops,
        "respawn_result",
        lambda job: (
            called.setdefault("job", job)
            and av_mod.agent_ops.RespawnResult("claude --resume x --bg", "p:1")
        ),
    )
    app, view = _make_view([_make_job()])
    view.handle_key("R")
    assert "job" in called
    assert app._submitted_actions == ["agent.respawn"]
    assert any("已重启" in m for m in app._notifications)


def test_r_key_refreshes_not_respawn(monkeypatch):
    # `r` is refresh on EVERY tab now; it must NOT respawn.
    respawned = {"n": 0}
    monkeypatch.setattr(
        av_mod.agent_ops,
        "respawn_result",
        lambda job: (
            respawned.__setitem__("n", respawned["n"] + 1)
            or av_mod.agent_ops.RespawnResult("x", "p:1")
        ),
    )
    app, view = _make_view([_make_job()])
    view.handle_key("r")
    assert respawned["n"] == 0
    assert any("刷新" in m for m in app._notifications)


def test_enter_key_tmux_takeover(monkeypatch):
    # Enter is the unified primary action; tmux-first (ADR-0001): a dead,
    # non-resident worker resumes inside tmux + enters.
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False),
    )
    app, view = _make_view([_make_job()])
    view.handle_key("enter")
    assert app.result is not None
    assert isinstance(app.result, TmuxResumeIntent)


def test_enter_key_resident_worker_attaches_in_place(monkeypatch):
    # A tmux-resident live worker is entered in place — no kill, no confirm.
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False, alive=True, tmux_target="proj:5"),
    )
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("enter")
    assert app.result == AttachIntent("proj:5")
    assert app._confirm_messages == []


def test_t_key_terminal_takeover_routes_to_resume_intent(monkeypatch):
    # t = 终端接回 (fallback): bare-terminal resume via the existing path.
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False),
    )
    app, view = _make_view([_make_job()])
    view.handle_key("t")
    assert app.result is not None
    assert isinstance(app.result, ResumeIntent)


def test_enter_and_t_refuse_current(monkeypatch):
    monkeypatch.setattr(
        av_mod.agent_ops, "resume_takeover", lambda job: _takeover_session(current=True)
    )
    app, view = _make_view([_make_job()])
    view.handle_key("enter")
    view.handle_key("t")
    assert app.result is None
    assert sum("不能接回当前会话" in m for m in app._notifications) == 2


def _takeover_session(current, alive=False, tmux_target=None):
    from cc_session_control.models import Session

    return Session(
        sid="x",
        cwd="/tmp",
        label="x",
        mtime=0.0,
        prompts=0,
        pid=999 if alive else None,
        alive=alive,
        current=current,
        source="bg",
        tmux_target=tmux_target,
    )


def test_enter_key_live_worker_confirms_takeover(monkeypatch):
    # B1: takeover of a RUNNING (non-resident) worker kills its host pid →
    # must confirm first, then resume inside tmux.
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False, alive=True),
    )
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("enter")
    assert app.result is None  # not resumed yet
    assert app._confirm_messages and "接回后台 agent" in app._confirm_messages[0]
    assert "终止原进程" in app._confirm_messages[0]
    app._last_confirm()  # simulate pressing y
    assert isinstance(app.result, TmuxResumeIntent)


def test_t_key_live_worker_confirms_terminal_takeover(monkeypatch):
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False, alive=True),
    )
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("t")
    assert app.result is None
    assert app._confirm_messages and "终端接回后台 agent" in app._confirm_messages[0]
    app._last_confirm()
    assert isinstance(app.result, ResumeIntent)


def test_enter_key_live_takeover_gated_when_degraded(monkeypatch):
    # R10: off /proc a live takeover can't safely kill the old pid — the view
    # must refuse BEFORE the confirm (not exit the TUI into do_resume's refusal).
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False, alive=True),
    )
    monkeypatch.setattr(av_mod.proc, "current_determinable", lambda: False)
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("enter")
    assert app.result is None
    assert app._confirm_messages == []  # refused before any confirm
    assert app._notifications[-1] == av_mod._DEGRADED


def test_enter_key_dead_worker_not_gated_when_degraded(monkeypatch):
    # A dead worker kills nothing — it stays resumable in degraded mode (B3).
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False, alive=False),
    )
    monkeypatch.setattr(av_mod.proc, "current_determinable", lambda: False)
    app, view = _make_view([_make_job()])
    view.handle_key("enter")
    assert isinstance(app.result, TmuxResumeIntent)


def test_enter_key_dead_worker_takes_over_directly(monkeypatch):
    monkeypatch.setattr(
        av_mod.agent_ops,
        "resume_takeover",
        lambda job: _takeover_session(current=False, alive=False),
    )
    app, view = _make_view([_make_job(host_alive=False)])
    view.handle_key("enter")
    assert app._confirm_messages == []  # dead worker: no takeover, no confirm
    assert isinstance(app.result, TmuxResumeIntent)


def test_d_key_refuses_live_job(monkeypatch):
    removed = {"n": 0}
    monkeypatch.setattr(
        av_mod.agent_ops,
        "remove_job",
        lambda job: removed.__setitem__("n", removed["n"] + 1) or True,
    )
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("d")
    assert removed["n"] == 0
    assert any("运行中的后台 agent 不能删除" in m for m in app._notifications)


def test_d_key_removes_settled_job(monkeypatch):
    from cc_session_control.data.removal import CleanupExecution

    monkeypatch.setattr(
        av_mod.agent_ops,
        "remove_job",
        lambda job: CleanupExecution(completed=[job.short]),
    )
    app, view = _make_view([_make_job(host_alive=False)])
    view.handle_key("d")
    assert app._submitted_actions == ["agent.remove"]
    assert any("已删除" in m for m in app._notifications)


def test_d_key_does_not_claim_success_when_artifact_removal_fails(
    monkeypatch,
    tmp_path,
):
    from cc_session_control.data.removal import (
        CleanupExecution,
        PathRemoval,
        RemovalStatus,
    )

    failed = CleanupExecution(
        removals=[PathRemoval(tmp_path / "job", RemovalStatus.FAILED, "denied")]
    )
    monkeypatch.setattr(av_mod.agent_ops, "remove_job", lambda job: failed)
    app, view = _make_view([_make_job(host_alive=False)])

    view.handle_key("d")

    assert "删除失败" in app._notifications[-1]
    assert "已删除" not in app._notifications[-1]


def test_s_key_stops_live_with_orphan_warning(monkeypatch):
    # Unified confirm: `s` on a live worker confirms first, then `_last_confirm()`
    # runs the stop body whose notify carries the orphan-risk warning.
    monkeypatch.setattr(av_mod.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(av_mod.agent_ops, "stop_job", lambda job: True)
    app, view = _make_view([_make_job(host_alive=True)])
    view.handle_key("s")
    assert app._confirm_messages  # a confirm is requested first
    app._last_confirm()  # simulate pressing y
    assert app._submitted_actions == ["agent.stop"]
    assert any("孤儿" in m for m in app._notifications)


def test_s_key_refuses_dead_worker(monkeypatch):
    monkeypatch.setattr(av_mod.proc, "current_determinable", lambda: True)
    stopped = {"n": 0}
    monkeypatch.setattr(
        av_mod.agent_ops,
        "stop_job",
        lambda job: stopped.__setitem__("n", stopped["n"] + 1) or True,
    )
    app, view = _make_view([_make_job(host_alive=False)])
    view.handle_key("s")
    assert stopped["n"] == 0
    assert app._confirm_messages == []  # guard fires before any confirm
    assert any("后台 agent 未在运行" in m for m in app._notifications)


def test_help_mode_and_return():
    app, view = _make_view([_make_job()])
    view.handle_key("?")
    assert view._mode == "help"
    # "其余" is honest: the footer prefix's Tab/q stay global and do NOT return.
    assert view.keyhints() == "其余任意键返回"
    view.handle_key("r")  # r keeps its prefix meaning: refresh, stay in help
    assert view._mode == "help"
    assert any("刷新" in m for m in app._notifications)
    view.handle_key("x")  # any other key returns
    assert view._mode == "list"
