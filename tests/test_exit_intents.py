"""ExitIntent status propagation through the public CLI seam."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from cc_session_control import cli
from cc_session_control.actions import session_ops
from cc_session_control.data import tmux
from cc_session_control.models import Session


def _session(*, alive: bool = False, pid: int | None = None) -> Session:
    return Session(
        sid="resume",
        cwd="/project",
        label="resume",
        mtime=1,
        prompts=1,
        pid=pid,
        alive=alive,
        current=False,
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
            "Failed to resume the session inside tmux "
            "(R10 degraded, or tmux unavailable).",
        ),
        (
            session_ops.TmuxNewIntent("/project"),
            "Failed to start a new session inside tmux (is tmux available?).",
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
    monkeypatch.setattr(tmux, "run_in_tmux", lambda *_args: None)

    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{message}\n"


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
    monkeypatch.setattr(tmux, "run_in_tmux", lambda *_args: "project:1")
    monkeypatch.setattr(tmux, "select_window", lambda _target: True)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(session_ops.os, "execvp", fail_exec)

    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert "tmux executable missing" in captured.err


def test_tui_terminal_resume_r10_refusal_exits_nonzero_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cc_session_control.data import proc

    _install_app(
        monkeypatch,
        session_ops.ResumeIntent(_session(alive=True, pid=4242)),
    )
    monkeypatch.setattr(proc, "current_determinable", lambda: False)

    assert cli.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Refused: '/proc' unavailable" in captured.err
    assert "can't be safely killed (R10)" in captured.err


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
    monkeypatch.setattr(tmux, "run_in_tmux", lambda *_args: None)
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
    monkeypatch.setattr(tmux, "run_in_tmux", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["cc_session_control"])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("cc_session_control.__main__", run_name="__main__")

    assert stopped.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to start a new session inside tmux" in captured.err
