"""Tests for project RC server discovery (Phase 5 / R5).

Covers the PURE cmdline matcher (`proc._match_rc_cmdline`, AC5), the
managed-vs-external classification in `rc.scan_servers_result` (by injecting a fake
managed-pid set and a fake `/proc` scan — no real `/proc` or tmux is stood up),
read-only `env_*` capture, and the `remoteControlSpawnMode` read on `rc.scan`.
"""

from __future__ import annotations

import json

from cc_session_control.config import cfg
from cc_session_control.data import proc, rc, rc_environment
from cc_session_control.data.proc import ProcIssue, ProcRC, ProcRCInventory
from cc_session_control.data.rc_enabled import (
    EnabledListOperation,
    EnabledListResult,
    EnabledListState,
)
from cc_session_control.data.tmux import TmuxIssue, TmuxWindow, WindowInventory
from cc_session_control.models import RCServer, RCStartupSettingState


def _nul(*argv: str) -> str:
    """Build a realistic NUL-separated /proc cmdline (trailing NUL included)."""
    return "\0".join(argv) + "\0"


def _created_target(target: str) -> rc.tmux.TmuxWriteResult:
    return rc.tmux.TmuxWriteResult(
        rc.tmux.TmuxWriteStage.NEW_WINDOW,
        rc.tmux.TmuxWriteState.SUCCEEDED,
        target=target,
    )


def _metadata_written(target: str) -> rc.tmux.TmuxWriteResult:
    return rc.tmux.TmuxWriteResult(
        rc.tmux.TmuxWriteStage.WINDOW_OPTION,
        rc.tmux.TmuxWriteState.SUCCEEDED,
        target=target,
    )


def _enabled_success(
    operation: EnabledListOperation,
    value,
    *,
    changed: bool = False,
) -> EnabledListResult:
    return EnabledListResult(
        operation,
        EnabledListState.SUCCEEDED,
        value,
        changed=changed,
        committed=changed,
    )


# --- AC5: pure cmdline matcher --------------------------------------------


def test_match_rc_server_nul_separated():
    cmd = _nul("claude", "remote-control", "--name", "ws/foo", "--spawn", "same-dir")
    m = proc._match_rc_cmdline("claude", cmd)
    assert m is not None
    assert m.name == "ws/foo"
    assert m.pid == 0  # filled by the scanner, not the matcher


def test_match_rc_server_space_joined():
    cmd = "claude remote-control --name ws/foo --spawn same-dir"
    m = proc._match_rc_cmdline("claude", cmd)
    assert m is not None and m.name == "ws/foo"


def test_match_rc_server_name_equals_form():
    cmd = _nul("/home/x/.local/bin/claude", "remote-control", "--name=ws/bar")
    m = proc._match_rc_cmdline("claude", cmd)
    assert m is not None and m.name == "ws/bar"


def test_match_node_launched_claude_by_argv0_basename():
    # comm may be `node`, but argv0 basename is still `claude` -> match on argv.
    cmd = _nul("/home/x/.local/share/claude/claude", "remote-control", "--name", "ws/z")
    m = proc._match_rc_cmdline("node", cmd)
    assert m is not None and m.name == "ws/z"


def test_match_excludes_codex_remote_control_flag():
    # codex uses --remote-control as a FLAG, argv0 `codex`, no subcommand token.
    cmd = _nul(
        "/home/x/.codex/packages/standalone/current/codex",
        "app-server",
        "--remote-control",
        "--listen",
        "unix://",
    )
    assert proc._match_rc_cmdline("codex", cmd) is None


def test_match_excludes_bare_interactive_claude():
    # A bare interactive claude collapses its cmdline to just `claude`.
    assert proc._match_rc_cmdline("claude", _nul("claude")) is None
    assert proc._match_rc_cmdline("claude", "claude") is None


def test_match_excludes_claude_without_remote_control():
    cmd = _nul("claude", "--name", "ws/foo")
    assert proc._match_rc_cmdline("claude", cmd) is None


def test_match_excludes_remote_control_without_name():
    cmd = _nul("claude", "remote-control", "--spawn", "same-dir")
    assert proc._match_rc_cmdline("claude", cmd) is None


def test_match_empty_cmdline():
    assert proc._match_rc_cmdline("", "") is None
    assert proc._match_rc_cmdline("", "\0\0") is None


# --- scan_rc_server_inventory degrades off Linux ---------------------------


def test_scan_rc_servers_degrades_without_proc(monkeypatch):
    monkeypatch.setattr(proc, "has_proc", lambda: False)
    assert proc.scan_rc_server_inventory().records == ()


# --- managed vs external classification (AC5) ------------------------------


def test_start_refuses_incomplete_window_inventory_without_spawning(
    tmp_path,
    monkeypatch,
):
    spawned: list[tuple] = []
    monkeypatch.setattr(
        rc,
        "project_trust",
        lambda _path: rc.ProjectTrustResult(rc.TrustDecision.TRUSTED, None),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory(
            issues=(TmuxIssue("tmux list-windows", None, "tmux timed out"),),
        ),
    )
    monkeypatch.setattr(
        rc.tmux,
        "run_in_tmux_result",
        lambda *args: spawned.append(args) or _created_target("rc:1"),
    )

    result = rc.start_one_result(str(tmp_path))

    assert result.state is rc.StartState.INVENTORY_UNAVAILABLE
    assert result.success is False
    assert "tmux timed out" in result.detail
    assert spawned == []


def test_start_retains_created_target_when_metadata_write_fails(
    tmp_path,
    monkeypatch,
):
    def run(argv, **_kwargs):
        if argv[1] == "has-session":
            return rc.tmux.subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "new-window":
            return rc.tmux.subprocess.CompletedProcess(argv, 0, "rc:7\n", "")
        if argv[1] == "set-option":
            return rc.tmux.subprocess.CompletedProcess(
                argv,
                2,
                "",
                "lost server connection\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(rc.tmux.subprocess, "run", run)

    result = rc._start_one_with_trust(
        str(tmp_path),
        rc.TrustDecision.TRUSTED,
        window_inventory=WindowInventory(),
    )

    assert result.state is rc.StartState.METADATA_FAILED
    assert result.success is False
    assert result.target == "rc:7"
    assert result.detail == "window-option: lost server connection"


def test_start_many_staggers_after_metadata_failure_with_created_target(
    monkeypatch,
):
    outcomes = iter(
        (
            rc.StartResult(
                rc.StartState.METADATA_FAILED,
                "/first",
                target="remote-control:1",
            ),
            rc.StartResult(
                rc.StartState.STARTED,
                "/second",
                target="remote-control:2",
            ),
        )
    )
    sleep_calls: list[int] = []
    monkeypatch.setattr(rc, "start_one_result", lambda _path: next(outcomes))
    monkeypatch.setattr(rc.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(cfg, "rc_stagger", 17)

    result = rc.start_many_result(["/first", "/second"])

    assert [item.state for item in result.results] == [
        rc.StartState.METADATA_FAILED,
        rc.StartState.STARTED,
    ]
    assert (result.started, result.failed) == (1, 1)
    assert sleep_calls == [17]


def test_start_many_does_not_stagger_after_failure_without_created_target(
    monkeypatch,
):
    outcomes = iter(
        (
            rc.StartResult(rc.StartState.TMUX_FAILED, "/first"),
            rc.StartResult(
                rc.StartState.STARTED,
                "/second",
                target="remote-control:2",
            ),
        )
    )
    sleep_calls: list[int] = []
    monkeypatch.setattr(rc, "start_one_result", lambda _path: next(outcomes))
    monkeypatch.setattr(rc.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(cfg, "rc_stagger", 17)

    result = rc.start_many_result(["/first", "/second"])

    assert [item.state for item in result.results] == [
        rc.StartState.TMUX_FAILED,
        rc.StartState.STARTED,
    ]
    assert (result.started, result.failed) == (1, 1)
    assert sleep_calls == []


def test_stop_fails_incomplete_window_inventory_without_killing(monkeypatch):
    killed: list[str] = []
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory(
            records=(TmuxWindow("@1", "project", False, 101, "/project"),),
            issues=(TmuxIssue("tmux list-windows", None, "malformed row 2"),),
        ),
    )
    monkeypatch.setattr(
        rc.tmux,
        "kill_window_result",
        lambda target: killed.append(target),
    )

    result = rc.stop_one_result("/project")

    assert result.state is rc.StopState.FAILED
    assert "malformed row 2" in result.detail
    assert killed == []


def test_project_status_is_unknown_when_window_inventory_is_incomplete(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    claude_json = _write_claude_json(
        tmp_path,
        {str(project): {"hasTrustDialogAccepted": True}},
    )
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(
        rc,
        "list_enabled_result",
        lambda: _enabled_success(EnabledListOperation.LIST, ()),
    )
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory(
            issues=(TmuxIssue("tmux list-windows", None, "lost server connection"),),
        ),
    )

    result = rc.scan_result()

    assert result.complete is False
    assert result.projects[0].status == "unknown"
    assert result.issues[0].detail == "lost server connection"


def test_scan_servers_classifies_managed_and_external(monkeypatch):
    # tmux owns window @1 whose pane pid is 111 -> managed; pid 222 is only
    # in /proc -> external.
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory((TmuxWindow("@1", "foo", False, 111, "/a"),)),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane_result",
        lambda target: rc.tmux.PaneCaptureResult(target),
    )
    monkeypatch.setattr(
        rc.proc,
        "scan_rc_server_inventory",
        lambda: ProcRCInventory(
            (ProcRC(111, "ws/foo", "/a"), ProcRC(222, "ws/bar", "/b"))
        ),
    )

    servers = rc.scan_servers_result(
        environment_cache=rc_environment.EnvironmentIdCache(),
    ).servers
    by_name = {s.name: s for s in servers}

    assert isinstance(servers[0], RCServer)
    assert by_name["ws/foo"].managed is True
    assert by_name["ws/foo"].pid == 111
    assert by_name["ws/foo"].status == "running"
    assert by_name["ws/bar"].managed is False
    assert by_name["ws/bar"].pid == 222
    assert by_name["ws/bar"].cwd == "/b"


def test_server_scan_retains_partial_proc_records_and_issues(monkeypatch):
    window = TmuxWindow("@1", "foo", False, 111, "/a")
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane_result",
        lambda target: rc.tmux.PaneCaptureResult(target),
    )

    result = rc.scan_servers_result(
        window_inventory=WindowInventory((window,)),
        proc_inventory=ProcRCInventory(
            records=(ProcRC(111, "ws/foo", "/a"),),
            issues=(
                ProcIssue(
                    "RC process inventory",
                    "/proc/222/cmdline",
                    "permission denied",
                ),
            ),
        ),
        environment_cache=rc_environment.EnvironmentIdCache(),
    )

    assert [server.name for server in result.servers] == ["ws/foo"]
    assert result.complete is False
    assert result.issues[0].path == "/proc/222/cmdline"
    assert "permission denied" in result.issues[0].detail


def test_scan_servers_managed_window_without_proc_match(monkeypatch):
    # tmux window present but the pid isn't in /proc (dead pane) -> still listed
    # managed, falling back to window name + path, status from pane_dead.
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory((TmuxWindow("@1", "foo", True, 111, "/a"),)),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane_result",
        lambda target: rc.tmux.PaneCaptureResult(target),
    )
    monkeypatch.setattr(
        rc.proc,
        "scan_rc_server_inventory",
        lambda: ProcRCInventory(),
    )

    servers = rc.scan_servers_result(
        environment_cache=rc_environment.EnvironmentIdCache(),
    ).servers
    assert len(servers) == 1
    assert servers[0].managed is True
    assert servers[0].name == "foo"
    assert servers[0].cwd == "/a"
    assert servers[0].status == "dead"


# --- read-only env_* capture ------------------------------------------------


def test_scan_servers_captures_env_id_without_ledger_side_effect(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    targets: list[str] = []
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory((TmuxWindow("@1", "foo", False, 111, "/a"),)),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane_result",
        lambda target: (
            targets.append(target)
            or rc.tmux.PaneCaptureResult(
                target,
                "starting...\nenvironment=env_abc123XYZ\nready",
            )
        ),
    )
    monkeypatch.setattr(
        rc.proc,
        "scan_rc_server_inventory",
        lambda: ProcRCInventory((ProcRC(111, "ws/foo", "/a"),)),
    )

    cache = rc_environment.EnvironmentIdCache()
    servers = rc.scan_servers_result(environment_cache=cache).servers
    next_servers = rc.scan_servers_result(environment_cache=cache).servers

    assert servers[0].env_id == "env_abc123XYZ"
    assert next_servers[0].env_id == "env_abc123XYZ"
    assert targets == ["@1"]  # addressed by unique window id, not name
    assert not cfg.environments_ledger.exists()


def test_scan_servers_returns_no_env_id_when_capture_has_none(monkeypatch):
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory((TmuxWindow("@1", "foo", False, 111, "/a"),)),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane_result",
        lambda target: rc.tmux.PaneCaptureResult(target, "no env here"),
    )
    monkeypatch.setattr(
        rc.proc,
        "scan_rc_server_inventory",
        lambda: ProcRCInventory((ProcRC(111, "ws/foo", "/a"),)),
    )

    servers = rc.scan_servers_result(
        environment_cache=rc_environment.EnvironmentIdCache(),
    ).servers
    assert servers[0].env_id is None


def test_successful_pane_capture_without_env_id_keeps_server_scan_complete(
    monkeypatch,
):
    window = TmuxWindow("@1", "foo", False, 111, "/a")
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane_result",
        lambda target: rc.tmux.PaneCaptureResult(target, "server starting"),
    )

    result = rc.scan_servers_result(
        window_inventory=WindowInventory((window,)),
        proc_inventory=ProcRCInventory((ProcRC(111, "ws/foo", "/a"),)),
        environment_cache=rc_environment.EnvironmentIdCache(),
    )

    assert result.complete is True
    assert result.issues == ()
    assert result.servers[0].env_id is None


def test_server_scan_retains_mixed_rows_ids_and_pane_capture_issue(monkeypatch):
    windows = (
        TmuxWindow("@1", "one", False, 111, "/a"),
        TmuxWindow("@2", "two", False, 222, "/b"),
    )
    targets: list[str] = []

    def capture(target: str) -> rc.tmux.PaneCaptureResult:
        targets.append(target)
        if target == "@1":
            return rc.tmux.PaneCaptureResult(
                target,
                "environment=env_KNOWN",
            )
        return rc.tmux.PaneCaptureResult(
            target,
            issue=rc.tmux.PaneCaptureIssue(
                "tmux capture-pane",
                target,
                "timed out after 5 seconds",
            ),
        )

    monkeypatch.setattr(rc, "_tmux_capture_pane_result", capture)

    result = rc.scan_servers_result(
        window_inventory=WindowInventory(windows),
        proc_inventory=ProcRCInventory(
            (
                ProcRC(111, "ws/one", "/a"),
                ProcRC(222, "ws/two", "/b"),
            ),
        ),
        environment_cache=rc_environment.EnvironmentIdCache(),
    )

    assert result.complete is False
    assert [(server.name, server.env_id) for server in result.servers] == [
        ("ws/one", "env_KNOWN"),
        ("ws/two", None),
    ]
    assert result.issues[0].source == "tmux capture-pane"
    assert result.issues[0].path == "/b [@2]"
    assert result.issues[0].detail == "timed out after 5 seconds"
    assert targets == ["@1", "@2"]


def test_start_stop_remove_and_stop_all_invalidate_capture_cache(
    tmp_path,
    monkeypatch,
):
    class CacheSpy:
        def __init__(self):
            self.all = 0
            self.windows: list[str] = []

        def invalidate_all(self):
            self.all += 1

        def invalidate_window(self, window_id):
            self.windows.append(window_id)

    cache = CacheSpy()
    project = str(tmp_path)
    window = TmuxWindow("@7", "project", False, 707, project)
    windows: list[TmuxWindow] = []
    monkeypatch.setattr(rc, "_environment_ids", cache)
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory(tuple(windows)),
    )
    monkeypatch.setattr(
        rc,
        "project_trust",
        lambda path: rc.ProjectTrustResult(rc.TrustDecision.TRUSTED, None),
    )
    monkeypatch.setattr(
        rc.tmux,
        "run_in_tmux_result",
        lambda *args: _created_target("rc:7"),
    )
    monkeypatch.setattr(
        rc.tmux,
        "set_window_option_result",
        lambda *args: _metadata_written("rc:7"),
    )

    result = rc.start_one_result(project)

    assert result.state is rc.StartState.STARTED
    assert cache.all == 1

    windows.append(window)
    monkeypatch.setattr(
        rc.tmux,
        "kill_window_result",
        lambda target: rc.tmux.KillResult(rc.tmux.KillState.KILLED, target),
    )
    assert rc.stop_one_result(project).success
    assert cache.windows == ["@7"]

    removed: list[str] = []
    monkeypatch.setattr(
        rc,
        "list_rm_result",
        lambda path: (
            removed.append(path)
            or _enabled_success(
                EnabledListOperation.REMOVE,
                True,
                changed=True,
            )
        ),
    )
    remove_result = rc.remove_one_result(project)
    assert remove_result.stop is not None and remove_result.stop.success
    assert removed == [project]
    assert cache.windows == ["@7", "@7"]

    monkeypatch.setattr(
        rc.tmux,
        "kill_session_result",
        lambda session: rc.tmux.KillResult(rc.tmux.KillState.KILLED, session),
    )
    assert rc.stop_all_result().success
    assert cache.all == 2


def test_stop_one_result_maps_kill_race_to_not_running(monkeypatch):
    window = TmuxWindow("@7", "project", False, 707, "/project")
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory((window,)),
    )
    monkeypatch.setattr(
        rc.tmux,
        "kill_window_result",
        lambda target: rc.tmux.KillResult(
            rc.tmux.KillState.TARGET_NOT_FOUND,
            target,
            "can't find window: @7",
        ),
    )

    result = rc.stop_one_result("/project")

    assert result.state is rc.StopState.NOT_RUNNING
    assert result.path == "/project"
    assert result.detail == "can't find window: @7"


def test_stop_one_result_retains_genuine_tmux_failure(monkeypatch):
    window = TmuxWindow("@7", "project", False, 707, "/project")
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory((window,)),
    )
    monkeypatch.setattr(
        rc.tmux,
        "kill_window_result",
        lambda target: rc.tmux.KillResult(
            rc.tmux.KillState.FAILED,
            target,
            "lost server connection",
        ),
    )

    result = rc.stop_one_result("/project")

    assert result.state is rc.StopState.FAILED
    assert result.detail == "lost server connection"


def test_stop_all_result_preserves_absent_failed_and_stopped(
    monkeypatch,
):
    class CacheSpy:
        def __init__(self):
            self.all = 0

        def invalidate_all(self):
            self.all += 1

    cache = CacheSpy()
    monkeypatch.setattr(rc, "_environment_ids", cache)
    monkeypatch.setattr(rc.cfg, "rc_session", "only-this-session")

    monkeypatch.setattr(
        rc.tmux,
        "kill_session_result",
        lambda session: rc.tmux.KillResult(
            rc.tmux.KillState.TARGET_NOT_FOUND,
            session,
            "can't find session: only-this-session",
        ),
    )
    missing = rc.stop_all_result()
    assert missing.state is rc.StopState.NOT_RUNNING
    assert missing.session == "only-this-session"
    assert missing.detail == "can't find session: only-this-session"
    assert cache.all == 0

    monkeypatch.setattr(
        rc.tmux,
        "kill_session_result",
        lambda session: rc.tmux.KillResult(
            rc.tmux.KillState.FAILED,
            session,
            "lost server connection",
        ),
    )
    failed = rc.stop_all_result()
    assert failed.state is rc.StopState.FAILED
    assert failed.detail == "lost server connection"
    assert cache.all == 0

    monkeypatch.setattr(
        rc.tmux,
        "kill_session_result",
        lambda session: rc.tmux.KillResult(rc.tmux.KillState.KILLED, session),
    )
    stopped = rc.stop_all_result()
    assert stopped.state is rc.StopState.STOPPED
    assert stopped.success is True
    assert cache.all == 1


def test_restart_continues_when_dead_window_vanishes_during_stop(
    tmp_path,
    monkeypatch,
):
    project = str(tmp_path)
    dead = TmuxWindow("@7", "project", True, 707, project)
    monkeypatch.setattr(
        rc,
        "stop_one_result",
        lambda path, *, window_inventory=None: rc.StopResult(
            rc.StopState.NOT_RUNNING,
            path,
            "can't find window: @7",
        ),
    )
    monkeypatch.setattr(
        rc.tmux,
        "run_in_tmux_result",
        lambda *_args: _created_target("rc:7"),
    )
    monkeypatch.setattr(
        rc.tmux,
        "set_window_option_result",
        lambda *_args: _metadata_written("rc:7"),
    )

    result = rc._start_one_with_trust(
        project,
        rc.TrustDecision.TRUSTED,
        window_inventory=WindowInventory((dead,)),
    )

    assert result.state is rc.StartState.STARTED


# --- remoteControlSpawnMode read (AC8 read half) ---------------------------


def _write_claude_json(tmp_path, projects):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"projects": projects}))
    return p


def test_scan_populates_spawn_mode(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    cj = _write_claude_json(
        tmp_path,
        {
            str(proj): {
                "hasTrustDialogAccepted": True,
                "remoteControlSpawnMode": "new-window",
            },
            str(other): {"hasTrustDialogAccepted": True},
        },
    )
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    monkeypatch.setattr(
        rc,
        "list_enabled_result",
        lambda: _enabled_success(EnabledListOperation.LIST, ()),
    )
    monkeypatch.setattr(rc, "_tmux_window_inventory", lambda: WindowInventory())
    # tmp_path is under the real temp root — neutralize the membership filter.
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())

    rows = {p.name: p for p in rc.scan_result().projects}
    assert rows["proj"].spawn_mode == "new-window"
    assert rows["other"].spawn_mode is None  # key present, mode unset


def test_scan_preserves_per_project_setting_failure_without_fallback(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "project"
    settings_dir = project / ".claude"
    settings_dir.mkdir(parents=True)
    local = settings_dir / "settings.local.json"
    local.write_text("{broken")
    (settings_dir / "settings.json").write_text(
        json.dumps({"remoteControlAtStartup": True})
    )
    claude_json = _write_claude_json(
        tmp_path,
        {str(project): {"hasTrustDialogAccepted": True}},
    )
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(
        rc,
        "list_enabled_result",
        lambda: _enabled_success(EnabledListOperation.LIST, ()),
    )
    monkeypatch.setattr(rc, "_tmux_window_inventory", lambda: WindowInventory())
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())

    [row] = rc.scan_result().projects

    assert row.rc_at_startup_setting.state is RCStartupSettingState.MALFORMED
    assert row.rc_at_startup_setting.source == local
    assert row.rc_at_startup is None


def test_order_by_activity_recent_first_never_active_sink():
    from cc_session_control.models import RCProject, Session, TrustDecision

    def proj(d):
        return RCProject(
            name=d.rsplit("/", 1)[-1],
            directory=d,
            trust_decision=TrustDecision.TRUSTED,
            in_list=False,
            status="stopped",
            auto_start=False,
        )

    def sess(cwd, mtime):
        return Session(
            sid="s",
            cwd=cwd,
            label="",
            mtime=mtime,
            prompts=0,
            pid=None,
            alive=False,
            current=False,
        )

    projects = [proj("/a"), proj("/b"), proj("/c"), proj("/z-never")]
    sessions = [
        sess("/b", 100.0),
        sess("/b", 30.0),  # max wins
        sess("/c", 50.0),
        sess("/c/sub", 999.0),  # subdir does NOT roll up
        sess("", 999.0),  # empty cwd ignored
    ]

    ordered = rc.order_by_activity(projects, sessions)
    assert [p.directory for p in ordered] == ["/b", "/c", "/a", "/z-never"]


def test_scan_marks_missing_directory(tmp_path, monkeypatch):
    """A missing-dir project stays listed (dir_exists=False) only while it is
    still actionable — in the autostart list or holding a tmux window. Pure
    trust residue (only ~/.claude.json references the deleted dir) is dropped:
    csctl can't act on it and never edits claude's files."""
    alive = tmp_path / "alive"
    alive.mkdir()
    deleted = str(tmp_path / "deleted")
    gone_running = str(tmp_path / "gone-running")
    gone_enabled = str(tmp_path / "gone-enabled")
    cj = _write_claude_json(
        tmp_path,
        {
            str(alive): {"hasTrustDialogAccepted": True},
            deleted: {"hasTrustDialogAccepted": True},
            gone_running: {"hasTrustDialogAccepted": True},
        },
    )
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    monkeypatch.setattr(
        rc,
        "list_enabled_result",
        lambda: _enabled_success(EnabledListOperation.LIST, (gone_enabled,)),
    )
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: WindowInventory(
            (TmuxWindow("@1", "gone-running", False, 5, gone_running),)
        ),
    )
    # tmp_path is under the real temp root — neutralize the membership filter.
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())

    rows = {p.directory: p for p in rc.scan_result().projects}
    assert set(rows) == {str(alive), gone_enabled, gone_running}
    assert deleted not in rows  # trust-only residue hidden
    assert rows[str(alive)].dir_exists is True
    assert rows[str(alive)].name == "alive"  # derived display name
    assert rows[gone_enabled].dir_exists is False  # stale rc-enabled entry
    assert rows[gone_running].dir_exists is False  # window survives dir removal
    assert rows[gone_running].status == "running"
