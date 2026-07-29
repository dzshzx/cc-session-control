"""Session-key preparations through the public App completion boundary."""

from __future__ import annotations

import threading

import pytest

import cc_session_control.app as app_mod
import cc_session_control.views.sessions as sessions_mod
from cc_session_control.actions.runner import ActionResult
from cc_session_control.actions.session_ops import (
    AttachIntent,
    ResumeIntent,
    TmuxResumeIntent,
)
from cc_session_control.app import App
from cc_session_control.data import proc
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.models import Session
from cc_session_control.views._confirm import DEGRADED
from cc_session_control.views.sessions import SessionsView


def _session(**overrides: object) -> Session:
    values: dict[str, object] = {
        "sid": "session-1",
        "cwd": "/tmp/project",
        "label": "session one",
        "mtime": 1.0,
        "prompts": 3,
        "pid": None,
        "alive": False,
        "current": False,
        "file": "/tmp/session-1.jsonl",
    }
    values.update(overrides)
    return Session(**values)


def _app_view(
    monkeypatch: pytest.MonkeyPatch,
    session: Session | None = None,
) -> tuple[
    App,
    SessionsView,
    threading.Event,
    list[tuple[int, str]],
]:
    ready = threading.Event()
    app = App()
    app._actions = app_mod.ActionRunner(ready.set)
    notifications: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, seconds=3: notifications.append(
            (threading.get_ident(), message)
        ),
    )
    monkeypatch.setattr(app, "_clear_notification", lambda: None)
    view = app.views[1]
    assert isinstance(view, SessionsView)
    if session is not None:
        view._all_sessions = [session]
        view._apply_filter()
        view._rebuild()
        view.walker.set_focus(0)
    return app, view, ready, notifications


def _complete_probe() -> proc.AncestorProbe:
    return proc.AncestorProbe(frozenset({1234}))


def _incomplete_probe() -> proc.AncestorProbe:
    issue = proc.ProcIssue("process ancestors", "/proc", "permission denied")
    return proc.AncestorProbe(frozenset(), (issue,))


def test_live_takeover_prepares_off_loop_then_confirms_and_exits_on_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(alive=True, pid=4242)
    app, view, ready, _notifications = _app_view(monkeypatch, session)
    main_thread = threading.get_ident()
    probe_threads: list[int] = []
    confirm_threads: list[int] = []
    exit_threads: list[int] = []
    confirmations: list[tuple[str, object]] = []
    exits: list[object] = []

    def probe() -> proc.AncestorProbe:
        probe_threads.append(threading.get_ident())
        return _complete_probe()

    def confirm(message: str, on_yes: object) -> None:
        confirm_threads.append(threading.get_ident())
        confirmations.append((message, on_yes))

    def exit_with(intent: object) -> None:
        exit_threads.append(threading.get_ident())
        exits.append(intent)

    monkeypatch.setattr(sessions_mod.proc, "probe_current_ancestors", probe)
    monkeypatch.setattr(app, "confirm", confirm)
    monkeypatch.setattr(app, "exit_with", exit_with)

    view.handle_key("enter")

    assert ready.wait(1)
    assert probe_threads and probe_threads[0] != main_thread
    assert confirmations == []
    assert exits == []

    assert app._on_action_pipe(b"prepared") is True
    assert len(probe_threads) == 1
    assert confirm_threads == [main_thread]
    assert "接回会话" in confirmations[0][0]
    assert exits == []

    on_yes = confirmations[0][1]
    assert callable(on_yes)
    on_yes()
    assert exit_threads == [main_thread]
    assert exits == [TmuxResumeIntent(session)]


def test_incomplete_takeover_probe_cannot_confirm_or_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(alive=True, pid=4242)
    app, view, ready, notifications = _app_view(monkeypatch, session)
    main_thread = threading.get_ident()
    probe_threads: list[int] = []
    confirms: list[str] = []
    exits: list[object] = []

    def probe() -> proc.AncestorProbe:
        probe_threads.append(threading.get_ident())
        return _incomplete_probe()

    monkeypatch.setattr(sessions_mod.proc, "probe_current_ancestors", probe)
    monkeypatch.setattr(
        app, "confirm", lambda message, _on_yes: confirms.append(message)
    )
    monkeypatch.setattr(app, "exit_with", exits.append)

    view.handle_key("enter")

    assert ready.wait(1)
    assert probe_threads and probe_threads[0] != main_thread
    assert app._on_action_pipe(b"prepared") is True
    assert len(probe_threads) == 1
    assert confirms == []
    assert exits == []
    assert notifications[-1] == (main_thread, DEGRADED)


def test_live_stop_prepares_off_loop_and_incomplete_evidence_cannot_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(alive=True, pid=4242)
    app, view, ready, notifications = _app_view(monkeypatch, session)
    main_thread = threading.get_ident()
    probe_threads: list[int] = []
    confirms: list[str] = []
    actions: list[str] = []

    def probe() -> proc.AncestorProbe:
        probe_threads.append(threading.get_ident())
        return _incomplete_probe()

    monkeypatch.setattr(sessions_mod.proc, "probe_current_ancestors", probe)
    monkeypatch.setattr(
        app, "confirm", lambda message, _on_yes: confirms.append(message)
    )
    monkeypatch.setattr(
        app,
        "submit_action",
        lambda action_key, _action: actions.append(action_key),
    )

    view.handle_key("s")

    assert ready.wait(1)
    assert probe_threads and probe_threads[0] != main_thread
    assert confirms == []
    assert actions == []

    assert app._on_action_pipe(b"prepared") is True
    assert confirms == []
    assert actions == []
    assert notifications[-1] == (main_thread, DEGRADED)


def test_dead_delete_prepares_off_loop_before_worker_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    app, view, ready, notifications = _app_view(monkeypatch, session)
    main_thread = threading.get_ident()
    probe_threads: list[int] = []
    delete_threads: list[int] = []
    delete_started = threading.Event()

    def probe() -> proc.AncestorProbe:
        probe_threads.append(threading.get_ident())
        return _complete_probe()

    def delete(_request: object) -> ActionResult:
        delete_threads.append(threading.get_ident())
        delete_started.set()
        return ActionResult.success("已删除")

    monkeypatch.setattr(sessions_mod.proc, "probe_current_ancestors", probe)
    monkeypatch.setattr(sessions_mod.tui_actions, "delete_session", delete)

    view.handle_key("d")

    assert ready.wait(1)
    assert probe_threads and probe_threads[0] != main_thread
    assert delete_threads == []

    ready.clear()
    assert app._on_action_pipe(b"prepared") is True
    assert delete_started.wait(1)
    assert delete_threads[0] != main_thread
    assert ready.wait(1)

    assert app._on_action_pipe(b"deleted") is True
    assert notifications[-1] == (main_thread, "已删除")


def test_cleanup_preview_pins_plan_while_probe_runs_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _session(sid="old", label="old generation", prompts=0)
    new = _session(sid="new", label="new generation", prompts=0)
    app, view, ready, _notifications = _app_view(monkeypatch)
    main_thread = threading.get_ident()
    probe_threads: list[int] = []
    old_plan = CleanupPlan(empty=[old])
    new_plan = CleanupPlan(empty=[new])
    view._plan = old_plan
    view._classified = old_plan.counts()
    view._enter_cleanup()

    def probe() -> proc.AncestorProbe:
        probe_threads.append(threading.get_ident())
        return _complete_probe()

    monkeypatch.setattr(sessions_mod.proc, "probe_current_ancestors", probe)

    view.handle_key("enter")

    assert ready.wait(1)
    assert probe_threads and probe_threads[0] != main_thread
    assert view._mode == "cleanup"
    view._plan = new_plan

    assert app._on_action_pipe(b"prepared") is True
    assert view._mode == "preview"
    assert view._preview is not None
    assert view._preview.targets == (old,)
    assert view._preview.plan is old_plan


def test_incomplete_cleanup_probe_cannot_open_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _session(prompts=0)
    app, view, ready, notifications = _app_view(monkeypatch)
    main_thread = threading.get_ident()
    view._plan = CleanupPlan(empty=[target])
    view._classified = view._plan.counts()
    view._enter_cleanup()
    monkeypatch.setattr(
        sessions_mod.proc,
        "probe_current_ancestors",
        _incomplete_probe,
    )

    view.handle_key("enter")

    assert ready.wait(1)
    assert view._mode == "cleanup"
    assert app._on_action_pipe(b"prepared") is True
    assert view._mode == "cleanup"
    assert view._preview is None
    assert notifications[-1] == (main_thread, DEGRADED)


def test_dead_fork_and_attach_resumes_do_not_probe_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, view, _ready, _notifications = _app_view(monkeypatch, _session())
    exits: list[object] = []
    monkeypatch.setattr(
        sessions_mod.proc,
        "probe_current_ancestors",
        lambda: (_ for _ in ()).throw(AssertionError("non-destructive path probed")),
    )
    monkeypatch.setattr(app, "exit_with", exits.append)

    view.handle_key("t")
    assert exits == [ResumeIntent(view._all_sessions[0])]

    live = _session(sid="fork-source", alive=True, pid=4242)
    view._all_sessions = [live]
    view._apply_filter()
    view._rebuild()
    view.walker.set_focus(0)
    view.handle_key("f")

    assert exits == [ResumeIntent(_session()), TmuxResumeIntent(live, fork=True)]

    resident = _session(
        sid="resident",
        alive=True,
        pid=4343,
        tmux_target="project:1",
    )
    view._all_sessions = [resident]
    view._apply_filter()
    view._rebuild()
    view.walker.set_focus(0)
    view.handle_key("enter")

    assert exits == [
        ResumeIntent(_session()),
        TmuxResumeIntent(live, fork=True),
        AttachIntent("project:1"),
    ]
