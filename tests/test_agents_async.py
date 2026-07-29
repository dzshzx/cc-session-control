"""AgentsView key-triggered reads stay off the urwid main loop."""

import threading

from cc_session_control.actions import agent_ops
from cc_session_control.actions.runner import ActionRunner
from cc_session_control.actions.session_ops import TmuxResumeIntent
from cc_session_control.app import App
from cc_session_control.models import AgentJob, Session
from cc_session_control.views.agents import AgentsView


def _job(*, host_alive: bool = False) -> AgentJob:
    return AgentJob(
        short="abcdef01",
        sid="abcdef0123456789",
        resume_sid="abcdef0123456789",
        state="idle",
        tempo="fast",
        cwd="/tmp/proj",
        name="worker",
        env_suffix="XYZ",
        respawn_flags=[],
        host_pid=999 if host_alive else None,
        host_alive=host_alive,
    )


def _prepared(*, alive: bool = False) -> agent_ops.TakeoverPreparationResult:
    return agent_ops.TakeoverPreparationResult(
        agent_ops.TakeoverPreparationState.READY,
        session=Session(
            sid="resume",
            cwd="/tmp/proj",
            label="worker",
            mtime=0.0,
            prompts=0,
            pid=999 if alive else None,
            alive=alive,
            current=False,
            source="bg",
        ),
    )


def _view_with_runner(job: AgentJob, ready: threading.Event) -> tuple[App, AgentsView]:
    app = App()
    view = app.views[2]
    assert isinstance(view, AgentsView)
    app._actions = ActionRunner(ready.set)
    app.notify = lambda *_args, **_kwargs: None
    view._jobs = (job,)
    view._rebuild()
    return app, view


def test_takeover_preparation_runs_off_loop_and_exit_applies_on_main(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    ready = threading.Event()
    worker_threads = []
    intents = []
    main_thread = threading.get_ident()

    def slow_prepare(_job):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(1)
        return _prepared()

    monkeypatch.setattr(agent_ops, "prepare_takeover", slow_prepare)
    app, view = _view_with_runner(_job(), ready)
    app.exit_with = lambda intent: intents.append((intent, threading.get_ident()))

    view.handle_key("enter")

    assert started.wait(1)
    assert worker_threads and worker_threads[0] != main_thread
    assert intents == []
    release.set()
    assert ready.wait(1)
    assert intents == []
    app._on_action_pipe(b"1")
    assert len(intents) == 1
    assert isinstance(intents[0][0], TmuxResumeIntent)
    assert intents[0][1] == main_thread


def test_live_takeover_confirmation_applies_only_on_main(monkeypatch):
    ready = threading.Event()
    worker_threads = []
    confirmations = []
    main_thread = threading.get_ident()

    def prepare(_job):
        worker_threads.append(threading.get_ident())
        return _prepared(alive=True)

    monkeypatch.setattr(agent_ops, "prepare_takeover", prepare)
    app, view = _view_with_runner(_job(host_alive=True), ready)
    app.confirm = lambda message, _on_yes: confirmations.append(
        (message, threading.get_ident())
    )

    view.handle_key("enter")

    assert ready.wait(1)
    assert worker_threads and worker_threads[0] != main_thread
    assert confirmations == []
    app._on_action_pipe(b"1")
    assert "终止原进程" in confirmations[0][0]
    assert confirmations[0][1] == main_thread


def test_timeline_read_runs_off_loop_and_overlay_applies_on_main(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    ready = threading.Event()
    worker_threads = []
    overlay_threads = []
    main_thread = threading.get_ident()

    def slow_watch(_job):
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(1)
        return agent_ops.TimelineReadResult(
            agent_ops.TimelineReadState.READY,
            ("line-1", "line-2"),
        )

    monkeypatch.setattr(agent_ops, "watch", slow_watch)
    app, view = _view_with_runner(_job(), ready)
    original_show_overlay = view._show_overlay

    def show_overlay(*args):
        overlay_threads.append(threading.get_ident())
        original_show_overlay(*args)

    view._show_overlay = show_overlay

    view.handle_key("w")

    assert started.wait(1)
    assert worker_threads and worker_threads[0] != main_thread
    assert view._mode == "list"
    assert overlay_threads == []
    release.set()
    assert ready.wait(1)
    assert view._mode == "list"
    app._on_action_pipe(b"1")
    assert view._mode == "watch"
    assert overlay_threads == [main_thread]
