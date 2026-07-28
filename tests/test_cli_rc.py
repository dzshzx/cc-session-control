"""Public CLI rendering for typed Remote Control outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_session_control import cli
from cc_session_control.data import rc


def test_rc_stop_one_reports_all_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    monkeypatch.setattr(
        rc,
        "stop_one_result",
        lambda path: rc.StopResult(rc.StopState.STOPPED, path),
    )

    assert cli.main(["rc", "stop", str(project)]) == 0
    assert capsys.readouterr().out == f"Stopped {project}\n"

    monkeypatch.setattr(
        rc,
        "stop_one_result",
        lambda path: rc.StopResult(rc.StopState.NOT_RUNNING, path),
    )
    assert cli.main(["rc", "stop", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Stopped" not in captured.out
    assert f"Not running: {project}" in captured.err

    monkeypatch.setattr(
        rc,
        "stop_one_result",
        lambda path: rc.StopResult(
            rc.StopState.FAILED,
            path,
            "lost server connection",
        ),
    )
    assert cli.main(["rc", "stop", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Stopped" not in captured.out
    assert f"Failed to stop {project}: lost server connection" in captured.err


def test_rc_stop_all_reports_all_states(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        rc,
        "stop_all_result",
        lambda: rc.StopAllResult(
            rc.StopState.STOPPED,
            "only-this-session",
        ),
    )
    assert cli.main(["rc", "stop", "all"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Stopped all\n"
    assert captured.err == ""

    monkeypatch.setattr(
        rc,
        "stop_all_result",
        lambda: rc.StopAllResult(
            rc.StopState.NOT_RUNNING,
            "only-this-session",
            "can't find session: only-this-session",
        ),
    )
    assert cli.main(["rc", "stop", "all"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "No RC servers are running\n"

    monkeypatch.setattr(
        rc,
        "stop_all_result",
        lambda: rc.StopAllResult(
            rc.StopState.FAILED,
            "only-this-session",
            "timed out",
        ),
    )
    assert cli.main(["rc", "stop", "all"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Failed to stop all RC servers: timed out\n"
