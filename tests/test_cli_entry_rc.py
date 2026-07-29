"""CLI `rc` subcommand family: status, add, rm, up."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from cli_entry_helpers import enabled_list as _enabled_list

from cc_session_control import cli
from cc_session_control.config import cfg
from cc_session_control.data import liveness, rc, sessions, tmux
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from cc_session_control.models import (
    RCProject,
    RCStartupSettingRead,
    RCStartupSettingState,
    Session,
    Status,
    TrustDecision,
)


def _settings(
    state: ProjectSettingsState = ProjectSettingsState.AVAILABLE,
    detail: str = "",
) -> ProjectSettingsResult:
    return ProjectSettingsResult(state, {}, detail)


def _project(
    path: Path,
    *,
    status: Status = "stopped",
    rc_at_startup_setting: RCStartupSettingRead | None = None,
) -> RCProject:
    setting = rc_at_startup_setting or RCStartupSettingRead(
        RCStartupSettingState.MISSING
    )
    return RCProject(
        name=path.name,
        directory=str(path),
        trusted=True,
        in_list=False,
        status=status,
        auto_start=False,
        rc_at_startup_setting=setting,
    )


def test_rc_status_orders_by_activity_and_marks_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older = replace(_project(tmp_path / "older", status="dead"), dir_exists=False)
    newer = _project(tmp_path / "newer", status="running")
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        rc,
        "scan_result",
        lambda: rc.RCScanResult([older, newer], _settings()),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(
            (
                Session(
                    sid="old",
                    cwd=older.directory,
                    label="old",
                    mtime=1,
                    prompts=1,
                    pid=None,
                    alive=False,
                    current=False,
                ),
                Session(
                    sid="new",
                    cwd=newer.directory,
                    label="new",
                    mtime=2,
                    prompts=1,
                    pid=None,
                    alive=False,
                    current=False,
                ),
            ),
        ),
    )

    assert cli.main(["rc", "status"]) == 0
    output = capsys.readouterr().out
    assert output.index("newer") < output.index("older")
    assert "[running]" in output
    assert "[dead   ]" in output
    assert "(directory missing)" in output


def test_rc_status_empty_and_unavailable_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        rc,
        "scan_result",
        lambda: rc.RCScanResult([], _settings()),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(),
    )

    assert cli.main(["rc", "status"]) == 0
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(
        rc,
        "scan_result",
        lambda: rc.RCScanResult(
            [],
            _settings(ProjectSettingsState.MALFORMED, "bad json"),
        ),
    )
    assert cli.main(["rc", "status"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Project settings unavailable: malformed: bad json" in captured.err


def test_rc_status_reports_project_setting_failure_and_keeps_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid = _project(tmp_path / "valid")
    source = tmp_path / "broken" / ".claude" / "settings.local.json"
    broken = _project(
        tmp_path / "broken",
        rc_at_startup_setting=RCStartupSettingRead(
            RCStartupSettingState.MALFORMED,
            source,
            "bad json",
        ),
    )
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        rc,
        "scan_result",
        lambda: rc.RCScanResult([broken, valid], _settings()),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(),
    )

    assert cli.main(["rc", "status"]) == 1
    captured = capsys.readouterr()
    assert "valid" in captured.out
    assert "broken" in captured.out
    assert broken.directory in captured.err
    assert str(source) in captured.err
    assert "malformed" in captured.err
    assert "bad json" in captured.err


def test_rc_add_rejects_missing_and_untrusted_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing"
    assert cli.main(["rc", "add", str(missing)]) == 1
    assert "No such directory" in capsys.readouterr().err

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        rc,
        "project_trust",
        lambda _path: rc.ProjectTrustResult(
            TrustDecision.UNTRUSTED,
            _settings(),
        ),
    )
    assert cli.main(["rc", "add", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Not trusted" in captured.err
    assert "Added to list" not in captured.out


def test_rc_add_rejects_unavailable_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        rc,
        "project_trust",
        lambda _path: rc.ProjectTrustResult(
            TrustDecision.UNAVAILABLE,
            _settings(ProjectSettingsState.UNREADABLE, "permission denied"),
        ),
    )

    assert cli.main(["rc", "add", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Project settings unavailable: unreadable: permission denied" in captured.err
    assert "Not trusted" not in captured.err


def test_rc_add_updates_list_and_reports_start_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(cfg, "rc_list", tmp_path / "config" / "rc-enabled")
    monkeypatch.setattr(
        rc,
        "project_trust",
        lambda _path: rc.ProjectTrustResult(
            TrustDecision.TRUSTED,
            _settings(),
        ),
    )
    monkeypatch.setattr(
        rc,
        "start_one_result",
        lambda path: rc.StartResult(rc.StartState.STARTED, path),
    )

    assert cli.main(["rc", "add", str(project)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Added to list: {project}" in captured.out
    assert f"Started RC server for {project}" in captured.out
    assert cfg.rc_list.read_text().splitlines() == [str(project)]

    monkeypatch.setattr(
        rc,
        "start_one_result",
        lambda path: rc.StartResult(rc.StartState.TMUX_FAILED, path),
    )
    assert cli.main(["rc", "add", str(project)]) == 1
    captured = capsys.readouterr()
    assert "RC server was not started: tmux-failed" in captured.err


def test_rc_add_reports_created_target_when_metadata_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(cfg, "rc_list", tmp_path / "config" / "rc-enabled")
    monkeypatch.setattr(
        rc,
        "project_trust",
        lambda _path: rc.ProjectTrustResult(
            TrustDecision.TRUSTED,
            _settings(),
        ),
    )
    monkeypatch.setattr(
        rc,
        "start_one_result",
        lambda path: rc.StartResult(
            rc.StartState.METADATA_FAILED,
            path,
            "window-option: lost server connection",
            target="rc:7",
        ),
    )

    assert cli.main(["rc", "add", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Started RC server" not in captured.out
    assert captured.err == (
        "RC server target rc:7 was created, but metadata was not written: "
        "window-option: lost server connection\n"
    )


def test_rc_rm_real_enabled_list_and_tmux_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(cfg, "rc_list", tmp_path / "config" / "rc-enabled")
    monkeypatch.setattr(cfg, "rc_session", "isolated-rc")
    rc.list_add_result(str(project))

    inventory = {
        "value": tmux.WindowInventory(
            (tmux.TmuxWindow("@7", "project", False, 101, str(project)),)
        )
    }
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: inventory["value"],
    )
    monkeypatch.setattr(
        tmux,
        "kill_window_result",
        lambda target: tmux.KillResult(tmux.KillState.KILLED, target),
    )

    assert cli.main(["rc", "rm", str(project)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Removed and stopped: {project}" in captured.out
    assert rc.list_enabled_result().value == ()

    rc.list_add_result(str(project))

    monkeypatch.setattr(
        tmux,
        "kill_window_result",
        lambda target: tmux.KillResult(
            tmux.KillState.FAILED,
            target,
            "tmux denied",
        ),
    )
    assert cli.main(["rc", "rm", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Removed and stopped" not in captured.out
    assert "failed to stop the RC window" in captured.err
    assert rc.list_enabled_result().value == ()

    rc.list_add_result(str(project))
    inventory["value"] = tmux.WindowInventory()
    monkeypatch.setattr(
        tmux,
        "kill_window_result",
        lambda target: tmux.KillResult(
            tmux.KillState.TARGET_NOT_FOUND,
            target,
            "can't find window: @7",
        ),
    )
    assert cli.main(["rc", "rm", str(project)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Removed from the enabled list (not running)" in captured.out
    assert rc.list_enabled_result().value == ()


def test_rc_up_empty_success_and_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        rc,
        "start_all_listed_result",
        lambda: rc.StartManyResult(enabled_list=_enabled_list(())),
    )
    assert cli.main(["rc", "up"]) == 0
    assert capsys.readouterr().out == "List is empty\n"

    monkeypatch.setattr(
        rc,
        "start_all_listed_result",
        lambda: rc.StartManyResult(
            started=1,
            unavailable=1,
            untrusted=1,
            failed=1,
            enabled_list=_enabled_list(("/one", "/two")),
        ),
    )
    assert cli.main(["rc", "up"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "Started 1 project(s)\n"
    assert "Project settings unavailable; refused 1 project(s)" in captured.err
    assert "Not trusted; refused 1 project(s)" in captured.err
    assert "Failed to start 1 project(s)" in captured.err

    monkeypatch.setattr(
        rc,
        "start_all_listed_result",
        lambda: rc.StartManyResult(
            started=2,
            enabled_list=_enabled_list(("/one", "/two")),
        ),
    )
    assert cli.main(["rc", "up"]) == 0
    assert capsys.readouterr().out == "Started 2 project(s)\n"


def test_rc_up_reports_each_typed_tmux_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = rc.StartResult(
        rc.StartState.TMUX_FAILED,
        "/project",
        "new-window: lost server connection",
    )
    monkeypatch.setattr(
        rc,
        "start_all_listed_result",
        lambda: rc.StartManyResult(
            failed=1,
            results=(failure,),
            enabled_list=_enabled_list(("/project",)),
        ),
    )

    assert cli.main(["rc", "up"]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "Failed to start 1 project(s)\n"
        "  /project [tmux-failed]: new-window: lost server connection\n"
    )


def test_rc_up_acknowledges_created_target_on_metadata_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = rc.StartResult(
        rc.StartState.METADATA_FAILED,
        "/project",
        "window-option: permission denied",
        target="rc:7",
    )
    monkeypatch.setattr(
        rc,
        "start_all_listed_result",
        lambda: rc.StartManyResult(
            failed=1,
            results=(failure,),
            enabled_list=_enabled_list(("/project",)),
        ),
    )

    assert cli.main(["rc", "up"]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "Failed to start 1 project(s)\n"
        "  /project [metadata-failed; target rc:7 created]: "
        "window-option: permission denied\n"
    )
