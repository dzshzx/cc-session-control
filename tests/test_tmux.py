"""Public typed outcomes at the tmux kill seam."""

from __future__ import annotations

import subprocess

from cc_session_control.data import tmux


def test_kill_window_result_distinguishes_missing_target(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: (
            calls.append(argv)
            or subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="can't find window: @9\n",
            )
        ),
    )

    result = tmux.kill_window_result("@9")

    assert result.state is tmux.KillState.TARGET_NOT_FOUND
    assert result.target == "@9"
    assert result.detail == "can't find window: @9"
    assert calls == [["tmux", "kill-window", "-t", "@9"]]


def test_kill_window_result_retains_nonzero_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            2,
            stdout="",
            stderr="lost server connection\n",
        ),
    )

    result = tmux.kill_window_result("@4")

    assert result.state is tmux.KillState.FAILED
    assert result.detail == "lost server connection"


def test_kill_session_result_maps_absent_tmux_server_to_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=(
                "error connecting to /tmp/tmux-1000/default "
                "(No such file or directory)\n"
            ),
        ),
    )

    result = tmux.kill_session_result("rc")

    assert result.state is tmux.KillState.TARGET_NOT_FOUND


def test_kill_session_result_retains_timeout(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["tmux", "kill-session"], 5)

    monkeypatch.setattr(tmux.subprocess, "run", timeout)

    result = tmux.kill_session_result("rc")

    assert result.state is tmux.KillState.FAILED
    assert result.detail == "tmux timed out after 5 seconds"


def test_bool_kill_compatibility_is_derived_from_typed_result(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux,
        "kill_window_result",
        lambda target: tmux.KillResult(tmux.KillState.KILLED, target),
    )
    assert tmux.kill_window("@1") is True

    monkeypatch.setattr(
        tmux,
        "kill_window_result",
        lambda target: tmux.KillResult(tmux.KillState.TARGET_NOT_FOUND, target),
    )
    assert tmux.kill_window("@1") is False
