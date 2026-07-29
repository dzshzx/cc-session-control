"""Public CLI rendering for typed Remote Control outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_session_control import cli
from cc_session_control.config import cfg
from cc_session_control.data import environments, liveness, rc, sessions
from cc_session_control.data.proc import ProcRC, ProcRCInventory
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from cc_session_control.data.rc_environment import EnvironmentIdCache
from cc_session_control.data.tmux import TmuxWindow, WindowInventory
from cc_session_control.models import EnvRecord, InventoryIssue, RCProject, Session


def test_rc_status_reports_unknown_inventory_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issue = InventoryIssue(
        "tmux list-windows",
        None,
        "lost server connection",
    )
    project = RCProject(
        name="project",
        directory="/project",
        trusted=True,
        in_list=True,
        status="unknown",
        auto_start=False,
    )
    monkeypatch.setattr(
        rc,
        "scan_result",
        lambda: rc.RCScanResult(
            [project],
            ProjectSettingsResult(ProjectSettingsState.MISSING, {}),
            (issue,),
        ),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda: sessions.SessionScanResult(),
    )

    assert cli.main(["rc", "status"]) == 1
    captured = capsys.readouterr()
    assert "[unknown]" in captured.out
    assert "RC inventory is partial" in captured.err
    assert "lost server connection" in captured.err


def test_rc_status_orders_known_rows_but_reports_partial_transcripts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older = RCProject("older", "/older", True, True, "dead", False)
    newer = RCProject("newer", "/newer", True, True, "running", False)
    rows = (
        Session("old", "/older", "old", 1, 1, None, False, False),
        Session("new", "/newer", "new", 2, 1, None, False, False),
    )
    issue = sessions.TranscriptIssue(
        "session transcript",
        "/runtime/projects/newer/unreadable.jsonl",
        "permission denied",
    )
    monkeypatch.setattr(
        rc,
        "scan_result",
        lambda: rc.RCScanResult(
            [older, newer],
            ProjectSettingsResult(ProjectSettingsState.AVAILABLE, {}),
        ),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda: sessions.SessionScanResult(rows, (issue,)),
    )

    assert cli.main(["rc", "status"]) == 1
    captured = capsys.readouterr()
    assert captured.out.index("newer") < captured.out.index("older")
    assert "transcript inventory is partial" in captured.err
    assert issue.path in captured.err
    assert issue.detail in captured.err
    assert "RC inventory is partial" not in captured.err


def test_env_reports_partial_rc_inventory_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issue = InventoryIssue(
        "RC process inventory",
        "/proc/4242/cmdline",
        "permission denied",
    )
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        rc,
        "scan_servers_result",
        lambda: rc.RCServerScanResult(issues=(issue,)),
    )
    monkeypatch.setattr(
        rc,
        "scan_servers",
        lambda: (_ for _ in ()).throw(
            AssertionError("records-only wrapper used by production CLI")
        ),
    )

    assert cli.main(["env"]) == 1
    captured = capsys.readouterr()
    assert "Current bridge environments (partial): 0" in captured.out
    assert "Orphan environments: unavailable" in captured.out
    assert "environment inventory is partial" in captured.err
    assert "/proc/4242/cmdline" in captured.err
    assert "permission denied" in captured.err


def test_env_pane_capture_failure_warns_without_ledger_write_or_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    environments.upsert([EnvRecord("env", "OLD", "")], now=1.0)
    original = cfg.environments_ledger.read_bytes()
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory((TmuxWindow("@1", "foo", False, 111, "/a"),)),
    )
    monkeypatch.setattr(
        rc.proc,
        "scan_rc_server_inventory",
        lambda: ProcRCInventory((ProcRC(111, "ws/foo", "/a"),)),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane_result",
        lambda target: rc.tmux.PaneCaptureResult(
            target,
            issue=rc.tmux.PaneCaptureIssue(
                "tmux capture-pane",
                target,
                "timed out after 5 seconds",
            ),
        ),
    )
    monkeypatch.setattr(rc, "_environment_ids", EnvironmentIdCache())
    monkeypatch.setattr(
        rc,
        "scan_servers",
        lambda: (_ for _ in ()).throw(
            AssertionError("records-only wrapper used by production CLI"),
        ),
    )

    assert cli.main(["env"]) == 1
    captured = capsys.readouterr()
    assert "Current bridge environments (partial): 0" in captured.out
    assert "Orphan environments: unavailable" in captured.out
    assert "env_OLD" not in captured.out
    assert "environment inventory is partial" in captured.err
    assert "tmux capture-pane" in captured.err
    assert "/a [@1]" in captured.err
    assert "timed out after 5 seconds" in captured.err
    assert cfg.environments_ledger.read_bytes() == original


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
