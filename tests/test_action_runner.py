"""ActionRunner public-interface tests; no timing guesses."""

from __future__ import annotations

import threading

from cc_session_control.actions.runner import (
    Accepted,
    ActionCompletion,
    ActionResult,
    ActionRunner,
    Busy,
    Closed,
)


def test_single_flight_rejects_same_and_cross_action_until_consumed() -> None:
    started = threading.Event()
    release = threading.Event()
    ready = threading.Event()
    calls: list[str] = []

    def blocking() -> ActionResult:
        calls.append("first")
        started.set()
        assert release.wait(1)
        return ActionResult("done", needs_refresh=True)

    runner = ActionRunner(ready.set)
    assert isinstance(runner.submit("session.stop", blocking), Accepted)
    assert started.wait(1)
    assert isinstance(
        runner.submit("session.stop", lambda: ActionResult("duplicate")),
        Busy,
    )
    assert isinstance(
        runner.submit("project.start", lambda: ActionResult("other")),
        Busy,
    )

    release.set()
    assert ready.wait(1)
    assert isinstance(
        runner.submit("agent.remove", lambda: ActionResult("too soon")),
        Busy,
    )
    assert runner.consume_result() == ActionResult(
        "done",
        needs_refresh=True,
    )
    assert calls == ["first"]

    accepted_again = runner.submit(
        "agent.remove",
        lambda: ActionResult("not allowed"),
    )
    assert isinstance(accepted_again, Accepted)


def test_typed_completion_survives_rejected_second_submission() -> None:
    started = threading.Event()
    release = threading.Event()
    ready = threading.Event()

    def prepare() -> ActionCompletion[str]:
        started.set()
        assert release.wait(1)
        return ActionCompletion("first preparation")

    runner = ActionRunner(ready.set)
    assert isinstance(runner.submit("agent.prepare", prepare), Accepted)
    assert started.wait(1)
    assert isinstance(
        runner.submit(
            "agent.watch",
            lambda: ActionCompletion("replacement"),
        ),
        Busy,
    )

    release.set()
    assert ready.wait(1)
    assert runner.consume_result() == ActionCompletion("first preparation")


def test_close_does_not_join_and_drops_late_completion() -> None:
    started = threading.Event()
    release = threading.Event()
    ready = threading.Event()

    def blocking() -> ActionResult:
        started.set()
        assert release.wait(1)
        return ActionResult("late", needs_refresh=True)

    runner = ActionRunner(ready.set)
    runner.submit("session.delete", blocking)
    assert started.wait(1)

    runner.close()
    assert isinstance(
        runner.submit("project.start", lambda: ActionResult("never")),
        Closed,
    )
    release.set()
    assert ready.wait(1)
    assert runner.consume_result() is None


def test_unexpected_exception_reaches_excepthook_and_runner_recovers(
    monkeypatch,
) -> None:
    hooked = threading.Event()
    hook_args: list[threading.ExceptHookArgs] = []
    ready = threading.Event()

    def hook(args: threading.ExceptHookArgs) -> None:
        hook_args.append(args)
        hooked.set()

    def broken() -> ActionResult:
        raise RuntimeError("programming bug")

    monkeypatch.setattr(threading, "excepthook", hook)
    runner = ActionRunner(ready.set)
    assert isinstance(runner.submit("broken", broken), Accepted)
    assert hooked.wait(1)
    assert ready.wait(1)
    assert isinstance(
        runner.submit("too-early", lambda: ActionResult("bad")),
        Busy,
    )
    assert runner.consume_result() is None
    assert hook_args[0].exc_type is RuntimeError
    assert str(hook_args[0].exc_value) == "programming bug"

    ready.clear()
    assert isinstance(
        runner.submit("recovered", lambda: ActionResult("ok")),
        Accepted,
    )
    assert ready.wait(1)
    assert runner.consume_result() == ActionResult("ok")
