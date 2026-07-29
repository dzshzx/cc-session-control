"""Public typed outcomes at the tmux kill seam."""

from __future__ import annotations

import subprocess

from cc_session_control.data import tmux


def test_window_inventory_retains_timeout(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["tmux", "list-windows"], 5)

    monkeypatch.setattr(tmux.subprocess, "run", timeout)

    result = tmux.list_windows_inventory("rc")

    assert result.records == ()
    assert result.complete is False
    assert result.issues[0].source == "tmux list-windows"
    assert result.issues[0].detail == "tmux timed out after 5 seconds"


def test_window_inventory_retains_generic_nonzero(monkeypatch) -> None:
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

    result = tmux.list_windows_inventory("rc")

    assert result.records == ()
    assert result.complete is False
    assert result.issues[0].detail == "lost server connection"


def test_window_inventory_maps_precise_missing_target_to_complete_empty(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="can't find session: rc\n",
        ),
    )

    result = tmux.list_windows_inventory("rc")

    assert result.records == ()
    assert result.complete is True
    assert result.issues == ()


def test_window_inventory_retains_records_and_flags_malformed_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout=("@1\tproject\t0\t101\t/work/project\t/work/project\nbroken-row\n"),
            stderr="",
        ),
    )

    result = tmux.list_windows_inventory("rc")

    assert result.records == (
        tmux.TmuxWindow("@1", "project", False, 101, "/work/project"),
    )
    assert result.complete is False
    assert result.issues[0].source == "tmux list-windows"
    assert "malformed row 2" in result.issues[0].detail


def test_residency_inventory_retains_list_panes_failure(monkeypatch) -> None:
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

    result = tmux.residency_inventory([4242])

    assert dict(result.targets) == {}
    assert result.complete is False
    assert result.issues[0].source == "tmux list-panes"
    assert result.issues[0].detail == "lost server connection"


def test_residency_inventory_retains_partial_target_and_ancestor_issue(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tmux,
        "list_panes_inventory",
        lambda: tmux.PaneInventory((tmux.TmuxPane("project:1", 4242),)),
    )
    proc_issue = tmux.proc.ProcIssue(
        "process ancestors",
        "/proc/4242/stat",
        "permission denied",
    )
    monkeypatch.setattr(
        tmux.proc,
        "probe_ancestors",
        lambda _pid: tmux.proc.AncestorProbe(frozenset({4242}), (proc_issue,)),
    )

    result = tmux.residency_inventory([4242])

    assert dict(result.targets) == {4242: "project:1"}
    assert result.complete is False
    assert result.issues[0].source == "process ancestors"
    assert result.issues[0].path == "/proc/4242/stat"
    assert result.issues[0].detail == "permission denied"


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


def test_run_in_tmux_result_retains_new_window_failure_detail(monkeypatch) -> None:
    def run(argv, **_kwargs):
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1] == "new-window":
            return subprocess.CompletedProcess(
                argv,
                2,
                stdout="",
                stderr="lost server connection\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result("project", "claude", "cmd")

    assert result.operation is tmux.TmuxWriteOperation.CREATE_TARGET
    assert result.stage is tmux.TmuxWriteStage.NEW_WINDOW
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.target is None
    assert result.detail == "lost server connection"


def test_run_in_tmux_result_retains_new_session_timeout(monkeypatch) -> None:
    def run(argv, **_kwargs):
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="can't find session: project\n",
            )
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result("project", "claude", "cmd")

    assert result.stage is tmux.TmuxWriteStage.NEW_SESSION
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.detail == "tmux timed out after 5 seconds"


def test_run_in_tmux_result_retains_spawn_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FileNotFoundError("tmux executable missing")
        ),
    )

    result = tmux.run_in_tmux_result("project", "claude", "cmd")

    assert result.stage is tmux.TmuxWriteStage.SESSION_PROBE
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.detail == "tmux executable missing"


def test_run_in_tmux_result_rejects_success_without_created_target(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            0,
            stdout="",
            stderr="",
        ),
    )

    result = tmux.run_in_tmux_result("project", "claude", "cmd")

    assert result.stage is tmux.TmuxWriteStage.NEW_WINDOW
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.detail == "tmux succeeded without printing the created target"


def test_set_window_option_result_retains_target_and_nonzero_detail(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv,
            2,
            stdout="",
            stderr="permission denied\n",
        ),
    )

    result = tmux.set_window_option_result("project:7", "@csctl_path", "/project")

    assert result.operation is tmux.TmuxWriteOperation.SET_WINDOW_OPTION
    assert result.stage is tmux.TmuxWriteStage.WINDOW_OPTION
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.target == "project:7"
    assert result.detail == "permission denied"
