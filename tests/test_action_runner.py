"""ActionRunner public-interface tests; no timing guesses."""

from __future__ import annotations

import threading

from cc_session_control.actions.runner import (
    Accepted,
    ActionResult,
    ActionRunner,
    ActionStatus,
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
        return ActionResult.success("done", needs_refresh=True)

    runner = ActionRunner(ready.set)
    assert isinstance(runner.submit("session.stop", blocking), Accepted)
    assert started.wait(1)
    assert isinstance(
        runner.submit("session.stop", lambda: ActionResult.success("duplicate")),
        Busy,
    )
    assert isinstance(
        runner.submit("project.start", lambda: ActionResult.success("other")),
        Busy,
    )

    release.set()
    assert ready.wait(1)
    assert isinstance(
        runner.submit("agent.remove", lambda: ActionResult.success("too soon")),
        Busy,
    )
    assert runner.consume_result() == ActionResult.success(
        "done", needs_refresh=True,
    )
    assert calls == ["first"]

    accepted_again = runner.submit(
        "agent.remove", lambda: ActionResult.refused("not allowed"),
    )
    assert isinstance(accepted_again, Accepted)


def test_result_variants_are_typed() -> None:
    assert ActionResult.success("ok").status is ActionStatus.SUCCESS
    assert ActionResult.partial("some").status is ActionStatus.PARTIAL
    assert ActionResult.refused("no").status is ActionStatus.REFUSED
    assert ActionResult.failure("bad").status is ActionStatus.FAILURE


def test_close_does_not_join_and_drops_late_completion() -> None:
    started = threading.Event()
    release = threading.Event()
    ready = threading.Event()

    def blocking() -> ActionResult:
        started.set()
        assert release.wait(1)
        return ActionResult.success("late", needs_refresh=True)

    runner = ActionRunner(ready.set)
    runner.submit("session.delete", blocking)
    assert started.wait(1)

    runner.close()
    assert isinstance(
        runner.submit("project.start", lambda: ActionResult.success("never")),
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
        runner.submit("too-early", lambda: ActionResult.success("bad")),
        Busy,
    )
    assert runner.consume_result() is None
    assert hook_args[0].exc_type is RuntimeError
    assert str(hook_args[0].exc_value) == "programming bug"

    ready.clear()
    assert isinstance(
        runner.submit("recovered", lambda: ActionResult.success("ok")),
        Accepted,
    )
    assert ready.wait(1)
    assert runner.consume_result() == ActionResult.success("ok")
