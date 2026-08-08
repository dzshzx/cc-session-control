"""Exact tmux navigation at the exit/exec boundary."""

import pytest

from cc_session_control.actions import session_ops
from cc_session_control.data import tmux


def test_enter_window_outside_tmux_attaches_to_exact_session(monkeypatch) -> None:
    class ExecReplaced(BaseException):
        pass

    calls: list[tuple[str, list[str]]] = []

    def replace_process(file: str, args: list[str]) -> None:
        calls.append((file, args))
        raise ExecReplaced

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux, "select_window", lambda _target: True)
    monkeypatch.setattr(session_ops.os, "execvp", replace_process)

    with pytest.raises(ExecReplaced):
        session_ops.enter_window("csctl:3")

    assert calls == [("tmux", ["tmux", "attach-session", "-t", "=csctl"])]
