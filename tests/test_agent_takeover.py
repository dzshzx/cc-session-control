"""Agent takeover safety through the public ``AgentsView`` seam."""

from collections.abc import Callable

import cc_session_control.views.agents as av_mod
from cc_session_control.actions.session_ops import (
    ExitIntent,
    ResumeIntent,
    TmuxResumeIntent,
)
from cc_session_control.data import liveness, proc
from cc_session_control.models import AgentJob, SessionProc
from cc_session_control.views.agents import AgentsView


class TakeoverApp:
    def __init__(self) -> None:
        self.result: ExitIntent | None = None
        self.notifications: list[str] = []
        self.confirm_messages: list[str] = []
        self.last_confirm: Callable[[], None] | None = None

    def notify(self, message: str, seconds: int = 3) -> None:
        self.notifications.append(message)

    def confirm(self, message: str, on_yes: Callable[[], None]) -> None:
        self.confirm_messages.append(message)
        self.last_confirm = on_yes

    def exit_with(self, intent: ExitIntent) -> None:
        self.result = intent

    def submit_completion(self, _action_key, action, on_complete):
        on_complete(action())


def _job(*, alive: bool = False) -> AgentJob:
    return AgentJob(
        short="abcdef01",
        sid="abcdef0123456789",
        resume_sid="abcdef0123456789",
        cwd="/tmp/proj",
        name="worker",
        host_pid=4242 if alive else None,
        host_alive=alive,
    )


def _view(job: AgentJob) -> tuple[TakeoverApp, AgentsView]:
    app = TakeoverApp()
    view = AgentsView(app)
    view._jobs = (job,)
    view._rebuild()
    return app, view


def test_enter_live_refuses_partial_registry_before_takeover_side_effects(
    monkeypatch,
):
    calls = {"snapshot": 0}
    evidence = liveness.LivenessSnapshot(
        issues=(
            liveness.LivenessIssue(
                "session registry",
                "/broken/session.json",
                "invalid JSON",
            ),
            liveness.LivenessIssue(
                "claude agents --json",
                None,
                "exit status 5",
            ),
        ),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    def unexpected(*_args, **_kwargs):
        raise AssertionError("refused preparation must have no side effects")

    monkeypatch.setattr(av_mod.agent_ops.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(
        av_mod.agent_ops.tmux,
        "find_session_window_result",
        unexpected,
    )
    app, view = _view(_job(alive=True))
    app.confirm = unexpected
    app.exit_with = unexpected

    view.handle_key("enter")

    assert calls["snapshot"] == 1
    assert "已拒绝接回" in app.notifications[-1]
    assert "session registry" in app.notifications[-1]
    assert "/broken/session.json" in app.notifications[-1]
    assert "invalid JSON" in app.notifications[-1]
    assert "claude agents --json" in app.notifications[-1]
    assert "exit status 5" in app.notifications[-1]


def test_terminal_live_refuses_unknown_proc_before_takeover_side_effects(
    monkeypatch,
):
    calls = {"snapshot": 0}
    evidence = liveness.LivenessSnapshot(
        issues=(
            liveness.LivenessIssue(
                "process liveness",
                "/proc/4242/stat",
                "permission denied",
            ),
        ),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    def unexpected(*_args, **_kwargs):
        raise AssertionError("refused preparation must have no side effects")

    monkeypatch.setattr(av_mod.agent_ops.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(
        av_mod.agent_ops.tmux,
        "find_session_window_result",
        unexpected,
    )
    app, view = _view(_job(alive=True))
    app.confirm = unexpected
    app.exit_with = unexpected

    view.handle_key("t")

    assert calls["snapshot"] == 1
    assert "已拒绝接回" in app.notifications[-1]
    assert "process liveness" in app.notifications[-1]
    assert "/proc/4242/stat" in app.notifications[-1]
    assert "permission denied" in app.notifications[-1]


def test_enter_live_takeover_uses_one_complete_generation(monkeypatch):
    calls = {"snapshot": 0, "tmux": []}
    evidence = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=4242,
                sid="abcdef0123456789",
                proc_start="generation-one",
                proc_alive=True,
            ),
        ),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    def unexpected(*_args, **_kwargs):
        raise AssertionError("complete preparation must not rescan liveness")

    monkeypatch.setattr(av_mod.agent_ops.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(av_mod.agent_ops.liveness, "live_session_procs", unexpected)
    monkeypatch.setattr(proc, "probe_current_ancestors", unexpected)
    monkeypatch.setattr(
        av_mod.agent_ops.tmux,
        "find_session_window_result",
        lambda pids: (
            calls["tmux"].append(pids) or av_mod.agent_ops.tmux.SessionWindowResult()
        ),
    )
    app, view = _view(_job(alive=True))

    view.handle_key("enter")

    assert calls == {"snapshot": 1, "tmux": [[4242]]}
    assert app.result is None
    assert len(app.confirm_messages) == 1
    assert app.last_confirm is not None
    app.last_confirm()
    assert isinstance(app.result, TmuxResumeIntent)
    assert app.result.session.pid == 4242
    assert app.result.session.proc_start == "generation-one"


def test_enter_live_host_gone_in_fresh_generation_resumes_without_takeover(
    monkeypatch,
):
    calls = {"snapshot": 0}
    evidence = liveness.LivenessSnapshot()

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    def unexpected(*_args, **_kwargs):
        raise AssertionError("gone host must not query tmux or confirm takeover")

    monkeypatch.setattr(av_mod.agent_ops.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(proc, "probe_current_ancestors", unexpected)
    monkeypatch.setattr(
        av_mod.agent_ops.tmux,
        "find_session_window_result",
        unexpected,
    )
    app, view = _view(_job(alive=True))
    app.confirm = unexpected

    view.handle_key("enter")

    assert calls["snapshot"] == 1
    assert app.confirm_messages == []
    assert isinstance(app.result, TmuxResumeIntent)
    assert app.result.session.alive is False
    assert app.result.session.pid is None
    assert app.result.session.current is False
    assert app.result.session.tmux_target is None


def test_enter_and_terminal_refuse_current_from_snapshot(monkeypatch):
    calls = {"snapshot": 0}
    evidence = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=4242,
                sid="abcdef0123456789",
                proc_start="777",
                proc_alive=True,
            ),
        ),
        cur=frozenset({4242}),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    def unexpected(*_args, **_kwargs):
        raise AssertionError("current takeover must not confirm or exit")

    monkeypatch.setattr(av_mod.agent_ops.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(proc, "probe_current_ancestors", unexpected)
    monkeypatch.setattr(
        av_mod.agent_ops.tmux,
        "find_session_window_result",
        lambda _pids: av_mod.agent_ops.tmux.SessionWindowResult(),
    )
    app, view = _view(_job(alive=True))
    app.confirm = unexpected
    app.exit_with = unexpected

    view.handle_key("enter")
    view.handle_key("t")

    assert calls["snapshot"] == 2
    assert sum("不能接回当前会话" in msg for msg in app.notifications) == 2


def test_enter_dead_without_host_skips_incomplete_liveness(monkeypatch):
    calls = {"snapshot": 0}
    evidence = liveness.LivenessSnapshot(
        issues=(
            liveness.LivenessIssue(
                "session registry",
                "/broken/session.json",
                "invalid JSON",
            ),
        ),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    def unexpected(*_args, **_kwargs):
        raise AssertionError("dead resume must not query liveness, tmux, or confirm")

    monkeypatch.setattr(av_mod.agent_ops.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(av_mod.agent_ops.liveness, "live_session_procs", unexpected)
    monkeypatch.setattr(proc, "probe_current_ancestors", unexpected)
    monkeypatch.setattr(
        av_mod.agent_ops.tmux,
        "find_session_window_result",
        unexpected,
    )
    app, view = _view(_job())
    app.confirm = unexpected

    view.handle_key("enter")

    assert calls["snapshot"] == 0
    assert app.confirm_messages == []
    assert isinstance(app.result, TmuxResumeIntent)
    assert app.result.session.sid == "abcdef0123456789"
    assert app.result.session.cwd == "/tmp/proj"
    assert app.result.session.label == "worker"
    assert app.result.session.agent_short == "abcdef01"
    assert app.result.session.alive is False
    assert app.result.session.pid is None
    assert app.result.session.current is False
    assert app.result.session.proc_start == ""
    assert app.result.session.tmux_target is None


def test_terminal_dead_without_host_skips_incomplete_liveness(monkeypatch):
    calls = {"snapshot": 0}
    evidence = liveness.LivenessSnapshot(
        issues=(
            liveness.LivenessIssue(
                "process liveness",
                "/proc/4242/stat",
                "permission denied",
            ),
        ),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    def unexpected(*_args, **_kwargs):
        raise AssertionError("dead resume must not query liveness, tmux, or confirm")

    monkeypatch.setattr(av_mod.agent_ops.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(av_mod.agent_ops.liveness, "live_session_procs", unexpected)
    monkeypatch.setattr(proc, "probe_current_ancestors", unexpected)
    monkeypatch.setattr(
        av_mod.agent_ops.tmux,
        "find_session_window_result",
        unexpected,
    )
    app, view = _view(_job())
    app.confirm = unexpected

    view.handle_key("t")

    assert calls["snapshot"] == 0
    assert app.confirm_messages == []
    assert isinstance(app.result, ResumeIntent)
    assert app.result.session.sid == "abcdef0123456789"
    assert app.result.session.cwd == "/tmp/proj"
    assert app.result.session.label == "worker"
    assert app.result.session.agent_short == "abcdef01"
    assert app.result.session.alive is False
    assert app.result.session.pid is None
    assert app.result.session.current is False
    assert app.result.session.proc_start == ""
    assert app.result.session.tmux_target is None
