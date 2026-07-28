"""Public CLI entry and dispatch behavior."""

from __future__ import annotations

import io
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from cc_session_control import cli, cli_rc
from cc_session_control.actions import session_ops, skill_ops
from cc_session_control.config import cfg
from cc_session_control.data import (
    environments,
    liveness,
    rc,
    sessions,
    tmux,
)
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from cc_session_control.models import (
    AgentJob,
    BridgeEnv,
    RCProject,
    Session,
    Status,
    TrustDecision,
)


def _settings(
    state: ProjectSettingsState = ProjectSettingsState.AVAILABLE,
    detail: str = "",
) -> ProjectSettingsResult:
    return ProjectSettingsResult(state, {}, detail)


def _project(path: Path, *, status: Status = "stopped") -> RCProject:
    return RCProject(
        name=path.name,
        directory=str(path),
        trusted=True,
        in_list=False,
        status=status,
        auto_start=False,
    )


def _session(sid: str, label: str) -> Session:
    return Session(
        sid=sid,
        cwd="/project",
        label=label,
        mtime=1,
        prompts=1,
        pid=None,
        alive=False,
        current=False,
    )


def test_main_accepts_argv_and_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sessions, "scan", lambda: [])

    assert cli.main(["resume"]) == 0
    assert capsys.readouterr().out == "No matching sessions.\n"


@pytest.mark.parametrize("argv", [["rc"], ["skill"]])
def test_nested_command_is_required(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)

    assert stopped.value.code == 2


@pytest.mark.parametrize("argv", [["unknown"], ["rc", "unknown"]])
def test_unknown_command_is_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)

    assert stopped.value.code == 2


def test_dispatch_rejects_namespace_without_a_registered_handler() -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.dispatch(
            Namespace(command="unregistered"),
            cli.build_parser(),
        )

    assert stopped.value.code == 2


def test_rc_handler_rejects_unknown_leaf(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = cli_rc.handle_rc(Namespace(rc_command="unregistered"))

    assert status == 2
    assert "Unknown rc command: unregistered" in capsys.readouterr().err


def test_handler_streams_are_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rc, "list_enabled", lambda: ["/one", "/two"])
    stdout = io.StringIO()
    stderr = io.StringIO()
    args = cli.build_parser().parse_args(["rc", "list"])

    status = cli.dispatch(
        args,
        cli.build_parser(),
        stdout=stdout,
        stderr=stderr,
    )

    assert status == 0
    assert stdout.getvalue() == "/one\n/two\n"
    assert stderr.getvalue() == ""


def test_rc_status_orders_by_activity_and_marks_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older = _project(tmp_path / "older", status="dead")
    newer = _project(tmp_path / "newer", status="running")
    older.dir_exists = False
    monkeypatch.setattr(
        rc,
        "scan_result",
        lambda: rc.RCScanResult([older, newer], _settings()),
    )
    monkeypatch.setattr(
        sessions,
        "scan",
        lambda: [
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
        ],
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
        rc,
        "scan_result",
        lambda: rc.RCScanResult([], _settings()),
    )
    monkeypatch.setattr(sessions, "scan", lambda: [])

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


def test_rc_rm_real_enabled_list_and_tmux_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(cfg, "rc_list", tmp_path / "config" / "rc-enabled")
    monkeypatch.setattr(cfg, "rc_session", "isolated-rc")
    rc.list_add(str(project))

    def successful_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["list-windows", "-t"]:
            stdout = f"@7\tproject\t0\t101\t{project}\t{project}\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(tmux, "_tmux_run", successful_tmux)

    assert cli.main(["rc", "rm", str(project)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Removed and stopped: {project}" in captured.out
    assert rc.list_enabled() == []

    rc.list_add(str(project))

    def failed_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["list-windows", "-t"]:
            return subprocess.CompletedProcess(
                args,
                0,
                f"@7\tproject\t0\t101\t{project}\t{project}\n",
                "",
            )
        return subprocess.CompletedProcess(args, 1, "", "tmux denied")

    monkeypatch.setattr(tmux, "_tmux_run", failed_tmux)
    assert cli.main(["rc", "rm", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Removed and stopped" not in captured.out
    assert "failed to stop the RC window" in captured.err
    assert rc.list_enabled() == []

    rc.list_add(str(project))
    monkeypatch.setattr(
        tmux,
        "_tmux_run",
        lambda args: subprocess.CompletedProcess(args, 1, "", "not found"),
    )
    assert cli.main(["rc", "rm", str(project)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Removed from the enabled list (not running)" in captured.out
    assert rc.list_enabled() == []


def test_rc_up_empty_success_and_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(rc, "list_enabled", lambda: [])
    assert cli.main(["rc", "up"]) == 0
    assert capsys.readouterr().out == "List is empty\n"

    monkeypatch.setattr(rc, "list_enabled", lambda: ["/one", "/two"])
    monkeypatch.setattr(
        rc,
        "start_many_result",
        lambda _paths: rc.StartManyResult(
            started=1,
            unavailable=1,
            untrusted=1,
            failed=1,
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
        "start_many_result",
        lambda _paths: rc.StartManyResult(started=2),
    )
    assert cli.main(["rc", "up"]) == 0
    assert capsys.readouterr().out == "Started 2 project(s)\n"


def test_rc_stop_one_reports_success_and_failure(
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
        lambda path: rc.StopResult(rc.StopState.TMUX_FAILED, path),
    )
    assert cli.main(["rc", "stop", str(project)]) == 1
    captured = capsys.readouterr()
    assert "Stopped" not in captured.out
    assert "tmux unavailable or returned nonzero" in captured.err


@pytest.mark.parametrize(
    "tmux_result",
    [
        None,
        subprocess.CompletedProcess(
            ["tmux", "kill-session"],
            1,
            "",
            "failed",
        ),
    ],
)
def test_rc_stop_all_never_claims_tmux_failure_as_success(
    tmux_result: subprocess.CompletedProcess[str] | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cfg, "rc_session", "only-this-session")
    monkeypatch.setattr(
        tmux,
        "_tmux_run",
        lambda args: calls.append(args) or tmux_result,
    )

    assert cli.main(["rc", "stop", "all"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to stop all RC servers" in captured.err
    assert calls == [["kill-session", "-t", "only-this-session"]]


def test_rc_stop_all_success_is_scoped_to_configured_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cfg, "rc_session", "only-this-session")
    monkeypatch.setattr(
        tmux,
        "_tmux_run",
        lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert cli.main(["rc", "stop", "all"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Stopped all\n"
    assert captured.err == ""
    assert calls == [["kill-session", "-t", "only-this-session"]]


def test_resume_keyword_page_limit_and_all_reach_public_renderer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sessions,
        "scan",
        lambda: [
            _session("sid-one", "apple one"),
            _session("sid-two", "apple two"),
            _session("sid-three", "banana"),
        ],
    )

    assert (
        cli.main(
            ["resume", "apple", "--page", "2", "--limit", "1"],
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "sid-two" in output
    assert "sid-one" not in output
    assert "-- page 2/2, 2 session(s) --" in output

    assert cli.main(["resume", "apple", "--all"]) == 0
    output = capsys.readouterr().out
    assert "sid-one" in output
    assert "sid-two" in output
    assert "sid-three" not in output
    assert "-- 2 session(s) --" in output


def test_skill_install_uninstall_and_refusal_use_real_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")

    assert cli.main(["skill", "install"]) == 0
    captured = capsys.readouterr()
    target = cfg.skills_dir / skill_ops.SKILL_NAME / "SKILL.md"
    assert captured.err == ""
    assert "Installed skill" in captured.out
    assert target.is_file()

    assert cli.main(["skill", "install"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Refused:" in captured.err

    assert cli.main(["skill", "uninstall"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Removed" in captured.out
    assert not target.parent.exists()

    assert cli.main(["skill", "uninstall"]) == 1
    assert "Not installed:" in capsys.readouterr().err


def test_skill_boundary_failure_is_visible_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_install(*, force: bool) -> tuple[bool, str]:
        raise OSError("read only")

    monkeypatch.setattr(skill_ops, "install", fail_install)

    assert cli.main(["skill", "install"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Skill operation failed: read only" in captured.err


def test_agents_empty_and_status_rendering(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )

    assert cli.main(["agents"]) == 0
    assert capsys.readouterr().out == "No background agents found.\n"

    jobs = [
        AgentJob(
            short="live-id",
            sid="sid-live",
            resume_sid="resume-live",
            cwd="/live",
            name="builder",
            tempo="fast",
            host_alive=True,
        ),
        AgentJob(
            short="done-id",
            sid="sid-done",
            resume_sid="resume-done",
            cwd="/done",
            state="settled",
        ),
    ]
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(agent_jobs=tuple(jobs)),
    )

    assert cli.main(["agents"]) == 0
    output = capsys.readouterr().out
    assert "live-id  [live]  tempo=fast  builder  /live" in output
    assert "done-id  [settled]  tempo=-  done-id  /done" in output


def test_agents_keeps_partial_inventory_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = AgentJob(
        short="partial",
        sid="sid-partial",
        resume_sid="sid-partial",
        cwd="/work",
        state="settled",
    )
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(
            agent_jobs=(job,),
            issues=(
                liveness.LivenessIssue(
                    "job registry",
                    "/runtime/jobs/broken/state.json",
                    "invalid JSON",
                ),
            ),
        ),
    )

    assert cli.main(["agents"]) == 1
    captured = capsys.readouterr()
    assert "partial  [settled]" in captured.out
    assert "Warning: agent inventory is partial" in captured.err
    assert "job registry" in captured.err
    assert "/runtime/jobs/broken/state.json" in captured.err
    assert "invalid JSON" in captured.err


def test_env_renders_current_and_orphan_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = environments.Reconciliation(
        current=[
            BridgeEnv(
                prefix="session",
                key="current",
                bound_sid="sid-current",
                status="current",
            ),
        ],
        orphans=[
            BridgeEnv(
                prefix="cse",
                key="orphan",
                bound_sid=None,
            ),
        ],
    )
    monkeypatch.setattr(rc, "scan_servers", lambda: [])
    monkeypatch.setattr(
        environments,
        "reconcile",
        lambda **_kwargs: result,
    )

    assert cli.main(["env"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Current bridge environments: 1" in captured.out
    assert "session_current  sid=sid-current" in captured.out
    assert "Orphan environments" in captured.out
    assert "cse_orphan  sid=-" in captured.out


def test_no_command_runs_tui_and_handles_no_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cc_session_control import app as app_mod

    events: list[str] = []

    class FakeApp:
        def run(self) -> None:
            events.append("loop")
            return None

    monkeypatch.setattr(app_mod, "App", FakeApp)

    assert cli.main([]) == 0
    assert events == ["loop"]


@pytest.mark.parametrize(
    ("intent_type", "intent"),
    [
        (
            session_ops.ResumeIntent,
            session_ops.ResumeIntent(_session("resume", "resume")),
        ),
        (
            session_ops.AttachIntent,
            session_ops.AttachIntent("project:1"),
        ),
        (
            session_ops.TmuxResumeIntent,
            session_ops.TmuxResumeIntent(_session("tmux", "tmux")),
        ),
        (
            session_ops.TmuxNewIntent,
            session_ops.TmuxNewIntent("/project"),
        ),
    ],
)
def test_every_exit_intent_finalizes_once_after_tui_loop(
    intent_type: type[session_ops.ExitIntent],
    intent: session_ops.ExitIntent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cc_session_control import app as app_mod

    events: list[str] = []

    class FakeApp:
        def run(self) -> session_ops.ExitIntent:
            events.append("loop")
            return intent

    monkeypatch.setattr(app_mod, "App", FakeApp)
    monkeypatch.setattr(
        intent_type,
        "run",
        lambda _self: events.append("intent"),
    )

    assert cli.main([]) == 0
    assert events == ["loop", "intent"]


def test_invalid_theme_is_argparse_error() -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--theme", "neon"])

    assert stopped.value.code == 2
