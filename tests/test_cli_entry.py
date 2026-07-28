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
    registry,
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
    EnvRecord,
    RCProject,
    RCStartupSettingRead,
    RCStartupSettingState,
    Session,
    SessionProc,
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
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(sessions, "scan", lambda _inputs: [])

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
        rc,
        "scan_result",
        lambda: rc.RCScanResult([broken, valid], _settings()),
    )
    monkeypatch.setattr(sessions, "scan", lambda: [])

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
    monkeypatch.setattr(
        tmux,
        "kill_window_result",
        lambda target: tmux.KillResult(tmux.KillState.KILLED, target),
    )

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
    assert rc.list_enabled() == []

    rc.list_add(str(project))
    monkeypatch.setattr(
        tmux,
        "_tmux_run",
        lambda args: subprocess.CompletedProcess(args, 1, "", "not found"),
    )
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


def test_resume_keyword_page_limit_and_all_reach_public_renderer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        sessions,
        "scan",
        lambda _inputs: [
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


@pytest.mark.parametrize(
    ("source_kind", "expected_source", "expected_path", "expected_detail"),
    [
        (
            "session",
            "session registry",
            "sessions/broken.json",
            "invalid schema",
        ),
        (
            "job",
            "job registry",
            "jobs/denied/state.json",
            "permission denied",
        ),
        (
            "agents",
            "claude agents --json",
            "claude agents --json",
            "invalid JSON",
        ),
    ],
)
def test_resume_malformed_or_unreadable_liveness_emits_no_actionable_command(
    source_kind: str,
    expected_source: str,
    expected_path: str,
    expected_detail: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: False)
    monkeypatch.setattr(liveness.proc, "ancestor_pids", lambda: set())
    if source_kind == "session":
        broken = cfg.sessions_dir / "broken.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{}")
    elif source_kind == "job":
        denied = cfg.jobs_dir / "denied" / "state.json"
        denied.parent.mkdir(parents=True)
        denied.write_text("{}")
        original_read = registry._read_document

        def deny_job(path: str) -> object:
            if path == str(denied):
                raise PermissionError("permission denied")
            return original_read(path)

        monkeypatch.setattr(registry, "_read_document", deny_job)
    agents_stdout = "{bad json" if source_kind == "agents" else "[]"
    monkeypatch.setattr(
        liveness.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=agents_stdout,
            stderr="",
        ),
    )
    actual_inputs = liveness.liveness_inputs
    snapshots = 0
    scans = 0

    def capture_evidence() -> liveness.LivenessSnapshot:
        nonlocal snapshots
        snapshots += 1
        return actual_inputs()

    def reject_scan(inputs: liveness.LivenessSnapshot) -> list[Session]:
        nonlocal scans
        scans += 1
        return [_session("unsafe", "must not render")]

    monkeypatch.setattr(liveness, "liveness_inputs", capture_evidence)
    monkeypatch.setattr(sessions, "scan", reject_scan)

    assert cli.main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "liveness evidence is incomplete" in captured.err
    assert expected_source in captured.err
    assert expected_path in captured.err
    assert expected_detail in captured.err
    assert "claude --resume" not in captured.err
    assert snapshots == 1
    assert scans == 0


def test_resume_complete_liveness_is_injected_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = liveness.LivenessSnapshot()
    snapshots = 0
    injected = []

    def capture_evidence() -> liveness.LivenessSnapshot:
        nonlocal snapshots
        snapshots += 1
        return evidence

    def capture_scan(inputs: liveness.LivenessSnapshot) -> list[Session]:
        injected.append(inputs)
        return [_session("safe", "complete evidence")]

    monkeypatch.setattr(liveness, "liveness_inputs", capture_evidence)
    monkeypatch.setattr(sessions, "scan", capture_scan)

    assert cli.main(["resume"]) == 0
    captured = capsys.readouterr()
    assert "claude --resume safe" in captured.out
    assert captured.err == ""
    assert snapshots == 1
    assert injected == [evidence]


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
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        environments,
        "reconcile",
        lambda _evidence, _servers: result,
    )

    assert cli.main(["env"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Current bridge environments: 1" in captured.out
    assert "session_current  sid=sid-current" in captured.out
    assert "Orphan environments" in captured.out
    assert "cse_orphan  sid=-" in captured.out


def test_env_incomplete_liveness_is_partial_without_orphan_or_ledger_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    environments.upsert(
        [
            EnvRecord(
                prefix="cse",
                key="OLD",
                bound_sid="sid-old",
            ),
        ],
        now=1.0,
    )
    original = cfg.environments_ledger.read_bytes()
    evidence = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=1,
                sid="sid-live",
                bridge="session_LIVE",
                proc_alive=True,
            ),
        ),
        issues=(
            liveness.LivenessIssue(
                "session registry",
                "/runtime/sessions/broken.json",
                "invalid JSON",
            ),
        ),
    )
    snapshots = 0

    def capture_evidence() -> liveness.LivenessSnapshot:
        nonlocal snapshots
        snapshots += 1
        return evidence

    monkeypatch.setattr(liveness, "liveness_inputs", capture_evidence)
    monkeypatch.setattr(rc, "scan_servers", lambda: [])

    assert cli.main(["env"]) == 1
    captured = capsys.readouterr()
    assert "Current bridge environments (partial): 1" in captured.out
    assert "session_LIVE  sid=sid-live" in captured.out
    assert "Orphan environments: unavailable" in captured.out
    assert "cse_OLD" not in captured.out
    assert "session registry" in captured.err
    assert "/runtime/sessions/broken.json" in captured.err
    assert "invalid JSON" in captured.err
    assert cfg.environments_ledger.read_bytes() == original
    assert snapshots == 1


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
        lambda _self: events.append("intent") or 0,
    )

    assert cli.main([]) == 0
    assert events == ["loop", "intent"]


def test_invalid_theme_is_argparse_error() -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--theme", "neon"])

    assert stopped.value.code == 2
