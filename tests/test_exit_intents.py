"""ExitIntent status propagation through the public CLI seam."""

from __future__ import annotations

import runpy
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cc_session_control import cli
from cc_session_control.actions import session_ops
from cc_session_control.data import liveness, sessions, tmux
from cc_session_control.models import Session


@pytest.fixture(autouse=True)
def _complete_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )


def _session(
    *,
    alive: bool = False,
    pid: int | None = None,
    tmux_inventory_complete: bool = True,
    tmux_inventory_detail: str = "",
) -> Session:
    return Session(
        sid="resume",
        cwd="/project",
        label="resume",
        mtime=1,
        prompts=1,
        pid=pid,
        alive=alive,
        current=False,
        tmux_inventory_complete=tmux_inventory_complete,
        tmux_inventory_detail=tmux_inventory_detail,
    )


def _install_execution_session(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> Session:
    fresh = (
        replace(session, proc_start="known-start")
        if session.alive and not session.proc_start
        else session
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult((fresh,)),
    )
    return fresh


def _created_target(target: str) -> tmux.TmuxWriteResult:
    return tmux.TmuxWriteResult(
        tmux.TmuxWriteStage.NEW_WINDOW,
        tmux.TmuxWriteState.SUCCEEDED,
        target=target,
    )


def _create_failure(
    stage: tmux.TmuxWriteStage,
    detail: str = "tmux unavailable",
) -> tmux.TmuxWriteResult:
    return tmux.TmuxWriteResult(
        stage,
        tmux.TmuxWriteState.FAILED,
        detail=detail,
    )


def _install_app(
    monkeypatch: pytest.MonkeyPatch,
    intent: session_ops.ExitIntent,
) -> None:
    from cc_session_control import app as app_mod

    class FakeApp:
        def run(self) -> session_ops.ExitIntent:
            return intent

    monkeypatch.setattr(app_mod, "App", FakeApp)


@pytest.mark.parametrize(
    ("intent", "message"),
    [
        (
            session_ops.TmuxResumeIntent(_session()),
            "Failed to resume the session inside tmux: new-window: tmux unavailable.",
        ),
        (
            session_ops.TmuxNewIntent("/project"),
            "Failed to start a new session inside tmux: new-session: tmux unavailable.",
        ),
    ],
)
def test_tui_tmux_spawn_failure_exits_nonzero_on_stderr(
    intent: session_ops.ExitIntent,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_app(monkeypatch, intent)
    stage = (
        tmux.TmuxWriteStage.NEW_SESSION
        if isinstance(intent, session_ops.TmuxNewIntent)
        else tmux.TmuxWriteStage.NEW_WINDOW
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *_args: _create_failure(stage),
    )

    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{message}\n"


def test_tmux_resume_reports_typed_create_failure_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def run(argv, **_kwargs):
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv,
            2,
            stdout="",
            stderr="lost server connection\n",
        )

    monkeypatch.setattr(tmux.subprocess, "run", run)

    assert session_ops.TmuxResumeIntent(_session()).run() == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "Failed to resume the session inside tmux: "
        "new-window: lost server connection.\n"
    )


def test_tmux_new_reports_typed_create_timeout_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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

    assert session_ops.TmuxNewIntent("/project").run() == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "Failed to start a new session inside tmux: "
        "new-session: tmux timed out after 5 seconds.\n"
    )


@pytest.mark.parametrize(
    ("intent", "message"),
    [
        (
            session_ops.AttachIntent("project:1"),
            "Failed to enter tmux window project:1",
        ),
        (
            session_ops.TmuxResumeIntent(_session()),
            "Session resumed in tmux window project:1, but attaching failed",
        ),
        (
            session_ops.TmuxNewIntent("/project"),
            "Session started in tmux window project:1, but attaching failed",
        ),
    ],
)
def test_tui_attach_exec_failure_exits_nonzero_with_context_on_stderr(
    intent: session_ops.ExitIntent,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_exec(_file: str, _args: list[str]) -> None:
        raise FileNotFoundError("tmux executable missing")

    _install_app(monkeypatch, intent)
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *_args: _created_target("project:1"),
    )
    monkeypatch.setattr(tmux, "select_window", lambda _target: True)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(session_ops.os, "execvp", fail_exec)

    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert "tmux executable missing" in captured.err


@pytest.mark.parametrize("pid", [4242, None])
def test_tui_terminal_resume_r10_refusal_exits_nonzero_on_stderr(
    pid: int | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    liveness_calls = 0

    def incomplete_liveness() -> liveness.LivenessSnapshot:
        nonlocal liveness_calls
        liveness_calls += 1
        return liveness.LivenessSnapshot(
            issues=(
                liveness.LivenessIssue(
                    "process ancestors",
                    "/proc",
                    "unavailable",
                ),
            ),
        )

    _install_app(
        monkeypatch,
        session_ops.ResumeIntent(_session(alive=True, pid=pid)),
    )
    monkeypatch.setattr(liveness, "liveness_inputs", incomplete_liveness)
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )

    assert cli.main([]) == 1
    assert liveness_calls == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Terminal resume did not occur for session resume: "
        "process ancestors at /proc: unavailable.\n"
    )


@pytest.mark.parametrize(
    ("takeover", "expected_status", "expected_side_effects", "expected_error"),
    [
        (
            session_ops.TakeOverOutcome(
                session_ops.TakeOverState.REFUSED,
                "ancestor chain indeterminate",
            ),
            1,
            [],
            (
                "Terminal resume did not occur for session resume: "
                "ancestor chain indeterminate.\n"
            ),
        ),
        (
            session_ops.TakeOverOutcome(
                session_ops.TakeOverState.FAILED,
                "permission denied",
            ),
            1,
            [],
            ("Terminal resume did not occur for session resume: permission denied.\n"),
        ),
        (
            session_ops.TakeOverOutcome(session_ops.TakeOverState.KILLED),
            0,
            ["chdir:/project", "exec:claude"],
            "",
        ),
        (
            session_ops.TakeOverOutcome(session_ops.TakeOverState.GONE),
            0,
            ["chdir:/project", "exec:claude"],
            "",
        ),
    ],
    ids=["refused", "sigterm-permission-error", "killed", "gone"],
)
def test_tui_live_terminal_resume_requires_successful_takeover_before_exec(
    takeover: session_ops.TakeOverOutcome,
    expected_status: int,
    expected_side_effects: list[str],
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    side_effects: list[str] = []
    session = _install_execution_session(
        monkeypatch,
        _session(alive=True, pid=4242),
    )
    _install_app(
        monkeypatch,
        session_ops.ResumeIntent(session),
    )
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: takeover,
    )
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        session_ops.os,
        "chdir",
        lambda path: side_effects.append(f"chdir:{path}"),
    )
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda file, _args: side_effects.append(f"exec:{file}"),
    )

    assert cli.main([]) == expected_status
    assert side_effects == expected_side_effects
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected_error


def test_live_terminal_resume_uses_execution_time_session_generation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = _session(alive=True, pid=4242)
    fresh = Session(
        sid=stale.sid,
        cwd="/fresh-project",
        label=stale.label,
        mtime=2,
        prompts=2,
        pid=9002,
        alive=True,
        current=False,
        proc_start="fresh-start",
    )
    liveness_calls = 0
    scan_calls = 0

    def read_liveness() -> liveness.LivenessSnapshot:
        nonlocal liveness_calls
        liveness_calls += 1
        return liveness.LivenessSnapshot()

    def scan_execution_generation(
        _inputs: liveness.LivenessSnapshot,
    ) -> sessions.SessionScanResult:
        nonlocal scan_calls
        scan_calls += 1
        return sessions.SessionScanResult((fresh,))

    monkeypatch.setattr(liveness, "liveness_inputs", read_liveness)
    monkeypatch.setattr(
        sessions,
        "scan_result",
        scan_execution_generation,
    )
    takeovers: list[tuple[int, str]] = []
    changed: list[str] = []
    executed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda pid, start: (
            takeovers.append((pid, start))
            or session_ops.TakeOverOutcome(session_ops.TakeOverState.KILLED)
        ),
    )
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(session_ops.os, "chdir", changed.append)
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda program, argv: executed.append((program, argv)),
    )

    assert session_ops.ResumeIntent(stale).run() == 0
    assert takeovers == [(9002, "fresh-start")]
    assert changed == ["/fresh-project"]
    assert executed == [("claude", ["claude", "--resume", "resume"])]
    assert (liveness_calls, scan_calls) == (1, 1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_live_tmux_resume_uses_execution_time_session_generation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = _session(alive=True, pid=4242)
    fresh = replace(
        stale,
        cwd="/fresh-project",
        pid=9002,
        proc_start="fresh-start",
    )
    _install_execution_session(monkeypatch, fresh)
    takeovers: list[tuple[int, str]] = []
    spawns: list[tuple[str, str, str]] = []
    entered: list[str] = []
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda pid, start: (
            takeovers.append((pid, start))
            or session_ops.TakeOverOutcome(session_ops.TakeOverState.KILLED)
        ),
    )
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda tmux_session, window, cmd: (
            spawns.append((tmux_session, window, cmd))
            or _created_target("fresh-project:3")
        ),
    )
    monkeypatch.setattr(tmux, "select_window", entered.append)
    monkeypatch.setattr(
        tmux,
        "switch_client",
        lambda target: entered.append(target) or True,
    )
    monkeypatch.setenv("TMUX", "resident")

    assert session_ops.TmuxResumeIntent(stale).run() == 0
    assert takeovers == [(9002, "fresh-start")]
    assert spawns == [
        (
            "fresh-project",
            "resume",
            "cd /fresh-project && claude --resume resume",
        )
    ]
    assert entered == ["fresh-project:3", "fresh-project:3"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_live_tmux_resume_enters_fresh_resident_target_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = _session(alive=True, pid=4242)
    fresh = replace(
        stale, pid=9002, proc_start="fresh-start", tmux_target="fresh-project:7"
    )
    _install_execution_session(monkeypatch, fresh)
    entered: list[str] = []
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)

    def fail_replacement(*_args: object) -> None:
        pytest.fail("must not replace fresh resident target")

    monkeypatch.setattr(session_ops, "take_over_result", fail_replacement)
    monkeypatch.setattr(tmux, "run_in_tmux_result", fail_replacement)
    monkeypatch.setattr(tmux, "select_window", entered.append)
    monkeypatch.setattr(
        tmux, "switch_client", lambda target: entered.append(target) or True
    )
    monkeypatch.setenv("TMUX", "resident")

    assert session_ops.TmuxResumeIntent(stale).run() == 0
    assert entered == ["fresh-project:7", "fresh-project:7"]
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_stale_pid_becoming_gone_never_authorizes_terminal_resume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = replace(
        _session(alive=True, pid=4242),
        proc_start="stale-start",
    )
    fresh = replace(stale, pid=9002, proc_start="fresh-start")
    _install_execution_session(monkeypatch, fresh)
    takeovers: list[tuple[int, str]] = []

    def take_over(pid: int, start: str) -> session_ops.TakeOverOutcome:
        takeovers.append((pid, start))
        if pid == stale.pid:
            return session_ops.TakeOverOutcome(session_ops.TakeOverState.GONE)
        return session_ops.TakeOverOutcome(
            session_ops.TakeOverState.REFUSED,
            "fresh generation refused",
        )

    monkeypatch.setattr(session_ops, "take_over_result", take_over)
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        session_ops.os,
        "chdir",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not chdir")),
    )
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )

    assert session_ops.ResumeIntent(stale).run() == 1
    assert takeovers == [(9002, "fresh-start")]
    captured = capsys.readouterr()
    assert "fresh generation refused" in captured.err


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("liveness", "process ancestors"),
        ("transcript", "session transcript"),
        ("missing", "missing session id"),
        ("ambiguous", "ambiguous session id"),
        ("current", "current session"),
        ("identity", "incomplete execution-time identity"),
        ("cwd", "no usable execution-time cwd"),
    ],
)
def test_live_resume_refusal_matrix_stops_all_execution_boundaries(
    case: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = replace(
        _session(alive=True, pid=4242),
        proc_start="stale-start",
    )
    fresh = replace(stale, pid=9002, proc_start="fresh-start")
    rows = (fresh,)
    if case == "missing":
        rows = ()
    elif case == "ambiguous":
        rows = (fresh, replace(fresh, cwd="/other-project"))
    elif case == "current":
        rows = (replace(fresh, current=True),)
    elif case == "identity":
        rows = (replace(fresh, pid=None, proc_start=""),)
    elif case == "cwd":
        rows = (replace(fresh, cwd="/missing-project"),)

    if case == "liveness":
        issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
        monkeypatch.setattr(
            liveness,
            "liveness_inputs",
            lambda: liveness.LivenessSnapshot(issues=(issue,)),
        )
        monkeypatch.setattr(
            sessions,
            "scan_result",
            lambda _inputs: (_ for _ in ()).throw(AssertionError("must not scan")),
        )
    else:
        monkeypatch.setattr(
            sessions,
            "scan_result",
            lambda _inputs: sessions.SessionScanResult(rows),
        )
        if case == "transcript":
            issue = sessions.TranscriptIssue(
                "session transcript",
                "/transcript",
                "unreadable",
            )
            monkeypatch.setattr(
                sessions,
                "scan_result",
                lambda _inputs: sessions.SessionScanResult(rows, (issue,)),
            )
    monkeypatch.setattr(
        session_ops.os.path,
        "isdir",
        lambda path: case != "cwd" or path != "/missing-project",
    )
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(
        session_ops.os,
        "chdir",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not chdir")),
    )
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    assert session_ops.ResumeIntent(stale).run() == 1
    assert session_ops.TmuxResumeIntent(stale).run() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err


def test_tui_live_terminal_resume_without_pid_fails_closed_before_exec(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    side_effects: list[str] = []
    session = _install_execution_session(
        monkeypatch,
        _session(alive=True, pid=None),
    )
    _install_app(
        monkeypatch,
        session_ops.ResumeIntent(session),
    )
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        session_ops.os,
        "chdir",
        lambda path: side_effects.append(f"chdir:{path}"),
    )
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda file, _args: side_effects.append(f"exec:{file}"),
    )

    assert cli.main([]) == 1
    assert side_effects == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "incomplete execution-time identity (pid)" in captured.err


@pytest.mark.parametrize("pid", [4242, None])
def test_tui_live_tmux_resume_refuses_incomplete_liveness_without_spawn(
    pid: int | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    liveness_calls = 0

    def incomplete_liveness() -> liveness.LivenessSnapshot:
        nonlocal liveness_calls
        liveness_calls += 1
        return liveness.LivenessSnapshot(
            issues=(
                liveness.LivenessIssue(
                    "process stat",
                    "/proc/4242/stat",
                    "permission denied",
                ),
            ),
        )

    _install_app(
        monkeypatch,
        session_ops.TmuxResumeIntent(_session(alive=True, pid=pid)),
    )
    monkeypatch.setattr(liveness, "liveness_inputs", incomplete_liveness)
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    assert cli.main([]) == 1
    assert liveness_calls == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Failed to resume the session inside tmux: "
        "process stat at /proc/4242/stat: permission denied.\n"
    )


def test_tui_dead_terminal_resume_skips_liveness_and_execs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ExecReplaced(BaseException):
        pass

    exec_calls: list[tuple[str, list[str]]] = []

    def replace_process(file: str, args: list[str]) -> None:
        exec_calls.append((file, args))
        raise ExecReplaced

    _install_app(monkeypatch, session_ops.ResumeIntent(_session()))
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(AssertionError("must not acquire liveness")),
    )
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(session_ops.os, "execvp", replace_process)

    with pytest.raises(ExecReplaced):
        cli.main([])

    assert exec_calls == [("claude", ["claude", "--resume", "resume"])]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_tui_dead_tmux_resume_skips_liveness_and_spawns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn_calls: list[tuple[str, str, str]] = []
    enter_calls: list[str] = []
    _install_app(monkeypatch, session_ops.TmuxResumeIntent(_session()))
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(AssertionError("must not acquire liveness")),
    )
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: (
            spawn_calls.append((session, window, cmd)) or _created_target("project:7")
        ),
    )
    monkeypatch.setattr(
        tmux, "select_window", lambda target: enter_calls.append(target)
    )
    monkeypatch.setattr(
        tmux,
        "switch_client",
        lambda target: enter_calls.append(target) or True,
    )
    monkeypatch.setenv("TMUX", "resident")

    assert cli.main([]) == 0
    assert spawn_calls == [
        ("project", "resume", "cd /project && claude --resume resume")
    ]
    assert enter_calls == ["project:7", "project:7"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_tui_live_fork_skips_incomplete_liveness_and_residency(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn_calls: list[tuple[str, str, str]] = []
    enter_calls: list[str] = []
    session = _session(
        alive=True,
        pid=4242,
        tmux_inventory_complete=False,
        tmux_inventory_detail="tmux list-panes unavailable",
    )
    _install_app(monkeypatch, session_ops.TmuxResumeIntent(session, fork=True))
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(AssertionError("must not acquire liveness")),
    )
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda tmux_session, window, cmd: (
            spawn_calls.append((tmux_session, window, cmd))
            or _created_target("project:8")
        ),
    )
    monkeypatch.setattr(
        tmux, "select_window", lambda target: enter_calls.append(target)
    )
    monkeypatch.setattr(
        tmux,
        "switch_client",
        lambda target: enter_calls.append(target) or True,
    )
    monkeypatch.setenv("TMUX", "resident")

    assert cli.main([]) == 0
    assert spawn_calls == [
        (
            "project",
            "resume-fork",
            "cd /project && claude --resume resume --fork-session",
        )
    ]
    assert enter_calls == ["project:8", "project:8"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_tui_tmux_resume_refuses_incomplete_residency_without_spawn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _install_execution_session(
        monkeypatch,
        _session(
            alive=True,
            pid=4242,
            tmux_inventory_complete=False,
            tmux_inventory_detail=("tmux list-panes: tmux timed out after 5 seconds"),
        ),
    )
    _install_app(
        monkeypatch,
        session_ops.TmuxResumeIntent(session),
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    monkeypatch.setattr(
        session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: True)

    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert "tmux list-panes" in captured.err
    assert "tmux timed out after 5 seconds" in captured.err


def test_tui_terminal_resume_exec_failure_exits_nonzero_with_context_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_exec(_file: str, _args: list[str]) -> None:
        raise FileNotFoundError("claude executable missing")

    _install_app(monkeypatch, session_ops.ResumeIntent(_session()))
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(session_ops.os, "execvp", fail_exec)

    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to resume session resume in the terminal" in captured.err
    assert "claude executable missing" in captured.err


def test_tui_successful_switch_client_returns_zero_without_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_app(monkeypatch, session_ops.AttachIntent("project:1"))
    monkeypatch.setattr(tmux, "select_window", lambda _target: True)
    monkeypatch.setattr(tmux, "switch_client", lambda _target: True)
    monkeypatch.setenv("TMUX", "resident")

    assert cli.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_tui_successful_exec_replaces_process_without_returning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ExecReplaced(BaseException):
        pass

    exec_calls: list[tuple[str, list[str]]] = []

    def replace_process(file: str, args: list[str]) -> None:
        exec_calls.append((file, args))
        raise ExecReplaced

    _install_app(monkeypatch, session_ops.ResumeIntent(_session()))
    monkeypatch.setattr(session_ops.os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(session_ops.os, "execvp", replace_process)

    with pytest.raises(ExecReplaced):
        cli.main([])

    assert exec_calls == [("claude", ["claude", "--resume", "resume"])]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_installed_console_entry_exits_with_tui_failure_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entrypoint = Path(sys.executable).with_name("csctl")
    assert entrypoint.is_file()
    _install_app(monkeypatch, session_ops.TmuxNewIntent("/project"))
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *_args: _create_failure(tmux.TmuxWriteStage.NEW_SESSION),
    )
    monkeypatch.setattr(sys, "argv", [str(entrypoint)])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(entrypoint), run_name="__main__")

    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to start a new session inside tmux" in captured.err


def test_module_entry_exits_with_tui_failure_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_app(monkeypatch, session_ops.TmuxNewIntent("/project"))
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *_args: _create_failure(tmux.TmuxWriteStage.NEW_SESSION),
    )
    monkeypatch.setattr(sys, "argv", ["cc_session_control"])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("cc_session_control.__main__", run_name="__main__")

    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to start a new session inside tmux" in captured.err
