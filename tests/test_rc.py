"""Tests for project RC server discovery (Phase 5 / R5).

Covers the PURE cmdline matcher (`proc._match_rc_cmdline`, AC5), the
managed-vs-external classification in `rc.scan_servers` (by injecting a fake
managed-pid set and a fake `/proc` scan — no real `/proc` or tmux is stood up),
read-only `env_*` capture, and the `remoteControlSpawnMode` read on `rc.scan`.
"""

from __future__ import annotations

import json

from cc_session_control.config import cfg
from cc_session_control.data import proc, rc, rc_environment
from cc_session_control.data.proc import ProcRC
from cc_session_control.data.tmux import TmuxWindow
from cc_session_control.models import RCServer, RCStartupSettingState


def _nul(*argv: str) -> str:
    """Build a realistic NUL-separated /proc cmdline (trailing NUL included)."""
    return "\0".join(argv) + "\0"


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


# --- scan_rc_servers degrades off Linux ------------------------------------


def test_scan_rc_servers_degrades_without_proc(monkeypatch):
    monkeypatch.setattr(proc, "has_proc", lambda: False)
    assert proc.scan_rc_servers() == []


# --- managed vs external classification (AC5) ------------------------------


def test_scan_servers_classifies_managed_and_external(monkeypatch):
    # tmux owns window @1 whose pane pid is 111 -> managed; pid 222 is only
    # in /proc -> external.
    monkeypatch.setattr(
        rc,
        "_tmux_windows",
        lambda: [TmuxWindow("@1", "foo", False, 111, "/a")],
    )
    monkeypatch.setattr(rc, "_capture_env_id", lambda target: "")
    monkeypatch.setattr(
        rc.proc,
        "scan_rc_servers",
        lambda: [ProcRC(111, "ws/foo", "/a"), ProcRC(222, "ws/bar", "/b")],
    )

    servers = rc.scan_servers(
        environment_cache=rc_environment.EnvironmentIdCache(),
    )
    by_name = {s.name: s for s in servers}

    assert isinstance(servers[0], RCServer)
    assert by_name["ws/foo"].managed is True
    assert by_name["ws/foo"].pid == 111
    assert by_name["ws/foo"].status == "running"
    assert by_name["ws/bar"].managed is False
    assert by_name["ws/bar"].pid == 222
    assert by_name["ws/bar"].cwd == "/b"


def test_scan_servers_managed_window_without_proc_match(monkeypatch):
    # tmux window present but the pid isn't in /proc (dead pane) -> still listed
    # managed, falling back to window name + path, status from pane_dead.
    monkeypatch.setattr(
        rc,
        "_tmux_windows",
        lambda: [TmuxWindow("@1", "foo", True, 111, "/a")],
    )
    monkeypatch.setattr(rc, "_capture_env_id", lambda target: "")
    monkeypatch.setattr(rc.proc, "scan_rc_servers", lambda: [])

    servers = rc.scan_servers(
        environment_cache=rc_environment.EnvironmentIdCache(),
    )
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
        "_tmux_windows",
        lambda: [TmuxWindow("@1", "foo", False, 111, "/a")],
    )
    monkeypatch.setattr(
        rc,
        "_tmux_capture_pane",
        lambda target: (
            targets.append(target) or "starting...\nenvironment=env_abc123XYZ\nready"
        ),
    )
    monkeypatch.setattr(
        rc.proc, "scan_rc_servers", lambda: [ProcRC(111, "ws/foo", "/a")]
    )

    cache = rc_environment.EnvironmentIdCache()
    servers = rc.scan_servers(environment_cache=cache)
    next_servers = rc.scan_servers(environment_cache=cache)

    assert servers[0].env_id == "env_abc123XYZ"
    assert next_servers[0].env_id == "env_abc123XYZ"
    assert targets == ["@1"]  # addressed by unique window id, not name
    assert not cfg.environments_ledger.exists()


def test_scan_servers_returns_no_env_id_when_capture_has_none(monkeypatch):
    monkeypatch.setattr(
        rc,
        "_tmux_windows",
        lambda: [TmuxWindow("@1", "foo", False, 111, "/a")],
    )
    monkeypatch.setattr(rc, "_tmux_capture_pane", lambda target: "no env here")
    monkeypatch.setattr(
        rc.proc, "scan_rc_servers", lambda: [ProcRC(111, "ws/foo", "/a")]
    )

    servers = rc.scan_servers(
        environment_cache=rc_environment.EnvironmentIdCache(),
    )
    assert servers[0].env_id is None


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
    monkeypatch.setattr(rc, "_environment_ids", cache)
    monkeypatch.setattr(rc, "_window_for", lambda path: None)
    monkeypatch.setattr(
        rc,
        "trust_decision",
        lambda path: rc.TrustDecision.TRUSTED,
    )
    monkeypatch.setattr(rc.tmux, "run_in_tmux", lambda *args: "rc:7")
    monkeypatch.setattr(rc.tmux, "set_window_option", lambda *args: True)

    result = rc.start_one_result(project)

    assert result.state is rc.StartState.STARTED
    assert cache.all == 1

    monkeypatch.setattr(rc, "_window_for", lambda path: window)
    monkeypatch.setattr(rc.tmux, "kill_window", lambda target: True)
    assert rc.stop_one(project)
    assert cache.windows == ["@7"]

    removed: list[str] = []
    monkeypatch.setattr(rc, "list_rm", lambda path: removed.append(path))
    assert rc.remove_one(project)
    assert removed == [project]
    assert cache.windows == ["@7", "@7"]

    monkeypatch.setattr(rc.tmux, "kill_session", lambda session: True)
    assert rc.stop_all()
    assert cache.all == 2


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
    monkeypatch.setattr(rc, "list_enabled", lambda: [])
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [])
    # tmp_path is under the real temp root — neutralize the membership filter.
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())

    rows = {p.name: p for p in rc.scan()}
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
    monkeypatch.setattr(rc, "list_enabled", lambda: [])
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [])
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())

    [row] = rc.scan_result().projects

    assert row.rc_at_startup_setting.state is RCStartupSettingState.MALFORMED
    assert row.rc_at_startup_setting.source == local
    assert row.rc_at_startup is None


def test_order_by_activity_recent_first_never_active_sink():
    from cc_session_control.models import RCProject, Session

    def proj(d):
        return RCProject(
            name=d.rsplit("/", 1)[-1],
            directory=d,
            trusted=True,
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
    monkeypatch.setattr(rc, "list_enabled", lambda: [gone_enabled])
    monkeypatch.setattr(
        rc,
        "_tmux_windows",
        lambda: [TmuxWindow("@1", "gone-running", False, 5, gone_running)],
    )
    # tmp_path is under the real temp root — neutralize the membership filter.
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())

    rows = {p.directory: p for p in rc.scan()}
    assert set(rows) == {str(alive), gone_enabled, gone_running}
    assert deleted not in rows  # trust-only residue hidden
    assert rows[str(alive)].dir_exists is True
    assert rows[str(alive)].name == "alive"  # derived display name
    assert rows[gone_enabled].dir_exists is False  # stale rc-enabled entry
    assert rows[gone_running].dir_exists is False  # window survives dir removal
    assert rows[gone_running].status == "running"
