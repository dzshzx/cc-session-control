"""Public typed outcomes at the tmux kill seam."""

from __future__ import annotations

import subprocess

import pytest

from cc_session_control.data import tmux


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


@pytest.mark.parametrize(
    ("literal", "exact_target"),
    [
        ("csc", "=csc"),  # must not prefix-match the reserved csctl session
        ("cs*", "=cs*"),  # must not be interpreted as a glob
        ("=csctl", "==csctl"),  # a leading '=' is part of the literal name
    ],
)
def test_run_in_tmux_treats_session_name_as_literal(
    literal: str,
    exact_target: str,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{literal}:3\t@3\n" if argv[1] == "new-window" else "",
            stderr="",
        )

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result(literal, "project/leaf", "cmd")

    assert result.target == f"{literal}:3"
    assert calls[0] == ["tmux", "has-session", "-t", exact_target]
    assert calls[1][calls[1].index("-t") + 1] == exact_target


@pytest.mark.parametrize(
    ("target", "exact_target"),
    [
        ("csctl:3", "=csctl:3"),
        ("@legacy:3", "=@legacy:3"),
        ("@7", "@7"),
    ],
)
def test_window_navigation_uses_exact_literal_target(
    target: str,
    exact_target: str,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: (
            calls.append(argv)
            or subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        ),
    )

    assert tmux.select_window(target) is True
    assert tmux.switch_client(target) is True

    assert calls == [
        ["tmux", "select-window", "-t", exact_target],
        ["tmux", "switch-client", "-t", exact_target],
    ]


def test_window_option_exactifies_non_id_target(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: (
            calls.append(argv)
            or subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        ),
    )

    assert tmux.set_window_option_result("@legacy:3", "@key", "value").success
    assert calls == [["tmux", "set-option", "-w", "-t", "=@legacy:3", "@key", "value"]]


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

    assert result.stage is tmux.TmuxWriteStage.NEW_WINDOW
    assert result.stage is tmux.TmuxWriteStage.NEW_WINDOW
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.target is None
    assert result.detail == "lost server connection"


def test_mobile_switch_sets_scoped_prefix2_when_unset(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        operation = argv[1]
        if operation == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if operation == "new-window":
            return subprocess.CompletedProcess(
                argv, 0, stdout="csctl:2\t@2\n", stderr=""
            )
        if operation == "list-sessions":
            return subprocess.CompletedProcess(argv, 0, stdout="$7\tcsctl\n", stderr="")
        if operation == "show-options":
            return subprocess.CompletedProcess(argv, 0, stdout="None\n", stderr="")
        if operation == "set-option":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result(
        "csctl", "project/codex", "codex", mobile_switch=True
    )

    assert result.success
    assert calls[-2:] == [
        ["tmux", "show-options", "-Aqv", "-t", "$7", "prefix2"],
        ["tmux", "set-option", "-t", "$7", "prefix2", "C-a"],
    ]


def test_mobile_switch_preserves_existing_prefix2(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        operation = argv[1]
        if operation == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if operation == "new-window":
            return subprocess.CompletedProcess(
                argv, 0, stdout="csctl:2\t@2\n", stderr=""
            )
        if operation == "list-sessions":
            return subprocess.CompletedProcess(argv, 0, stdout="$7\tcsctl\n", stderr="")
        if operation == "show-options":
            return subprocess.CompletedProcess(argv, 0, stdout="C-g\n", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result(
        "csctl", "project/codex", "codex", mobile_switch=True
    )

    assert result.success
    assert calls[-1] == [
        "tmux",
        "show-options",
        "-Aqv",
        "-t",
        "$7",
        "prefix2",
    ]


def test_mobile_switch_prefix_failure_keeps_spawn_and_diagnostic(monkeypatch) -> None:
    def run(argv, **_kwargs):
        operation = argv[1]
        if operation == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if operation == "new-window":
            return subprocess.CompletedProcess(
                argv, 0, stdout="csctl:2\t@2\n", stderr=""
            )
        if operation == "list-sessions":
            return subprocess.CompletedProcess(argv, 0, stdout="$7\tcsctl\n", stderr="")
        if operation == "show-options":
            return subprocess.CompletedProcess(argv, 0, stdout="None\n", stderr="")
        if operation == "set-option":
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="permission denied\n"
            )
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result(
        "csctl", "project/codex", "codex", mobile_switch=True
    )

    assert result.success
    assert result.target == "csctl:2"
    assert result.detail == "mobile switch prefix: permission denied"


def test_run_in_tmux_result_recovers_when_another_process_creates_session(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []
    probe_count = 0

    def run(argv, **_kwargs):
        nonlocal probe_count
        calls.append(argv)
        if argv[1] == "has-session":
            probe_count += 1
            return subprocess.CompletedProcess(
                argv,
                0 if probe_count == 2 else 1,
                stdout="",
                stderr="" if probe_count == 2 else "can't find session: csctl\n",
            )
        if argv[1] == "new-session":
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="duplicate session: csctl\n",
            )
        if argv[1] == "new-window":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="csctl:1\t@42\n",
                stderr="",
            )
        if argv[1] == "set-option":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result(
        "csctl",
        "project/leaf",
        "cmd",
        sid="session-id",
        provider="claude",
    )

    assert result.success
    assert result.target == "csctl:1"
    assert result.window_id == "@42"
    assert calls == [
        ["tmux", "has-session", "-t", "=csctl"],
        [
            "tmux",
            "new-session",
            "-d",
            "-P",
            "-F",
            tmux._TARGET_FMT,
            "-s",
            "csctl",
            "-n",
            "project/leaf",
            "cmd",
        ],
        ["tmux", "has-session", "-t", "=csctl"],
        [
            "tmux",
            "new-window",
            "-P",
            "-F",
            tmux._TARGET_FMT,
            "-t",
            "=csctl",
            "-n",
            "project/leaf",
            "cmd",
        ],
        ["tmux", "set-option", "-w", "-t", "@42", "@csctl_provider", "claude"],
        [
            "tmux",
            "set-option",
            "-w",
            "-t",
            "@42",
            "@csctl_sid",
            "session-id",
        ],
    ]


def test_run_in_tmux_result_does_not_recover_new_session_without_target(
    monkeypatch,
) -> None:
    calls: list[str] = []
    probe_count = 0

    def run(argv, **_kwargs):
        nonlocal probe_count
        operation = argv[1]
        calls.append(operation)
        if operation == "has-session":
            probe_count += 1
            return subprocess.CompletedProcess(
                argv,
                0 if probe_count == 2 else 1,
                stdout="",
                stderr="" if probe_count == 2 else "can't find session: csctl\n",
            )
        if operation == "new-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if operation == "new-window":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="csctl:1\t@42\n",
                stderr="",
            )
        if operation == "set-option":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result(
        "csctl",
        "project/leaf",
        "cmd",
        sid="session-id",
        provider="claude",
    )

    assert result.stage is tmux.TmuxWriteStage.NEW_SESSION
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.target is None
    assert result.detail == "tmux succeeded without printing the created target"
    assert calls == ["has-session", "new-session"]


def test_run_in_tmux_result_retains_new_session_timeout(monkeypatch) -> None:
    calls: list[str] = []
    probe_count = 0

    def run(argv, **_kwargs):
        nonlocal probe_count
        operation = argv[1]
        calls.append(operation)
        if operation == "has-session":
            probe_count += 1
            return subprocess.CompletedProcess(
                argv,
                0 if probe_count == 2 else 1,
                stdout="",
                stderr="" if probe_count == 2 else "can't find session: csctl\n",
            )
        if operation == "new-session":
            raise subprocess.TimeoutExpired(argv, 5)
        if operation == "new-window":
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="csctl:1\t@42\n",
                stderr="",
            )
        if operation == "set-option":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", run)

    result = tmux.run_in_tmux_result(
        "csctl",
        "project/leaf",
        "cmd",
        sid="session-id",
        provider="claude",
    )

    assert result.stage is tmux.TmuxWriteStage.NEW_SESSION
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.target is None
    assert result.detail == "tmux timed out after 5 seconds"
    assert calls == ["has-session", "new-session"]


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

    assert result.stage is tmux.TmuxWriteStage.WINDOW_OPTION
    assert result.stage is tmux.TmuxWriteStage.WINDOW_OPTION
    assert result.state is tmux.TmuxWriteState.FAILED
    assert result.target == "project:7"
    assert result.detail == "permission denied"
