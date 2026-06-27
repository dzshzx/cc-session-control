"""Tests for project RC server discovery (Phase 5 / R5).

Covers the PURE cmdline matcher (`proc._match_rc_cmdline`, AC5), the
managed-vs-external classification in `rc.scan_servers` (by injecting a fake
managed-pid set and a fake `/proc` scan — no real `/proc` or tmux is stood up),
the one-way `env_*` capture into the ledger, and the `remoteControlSpawnMode`
read on `rc.scan`.
"""

from __future__ import annotations

import json

from cc_session_control.data import proc, rc
from cc_session_control.data.proc import ProcRC
from cc_session_control.models import EnvRecord, RCServer


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
        "app-server", "--remote-control", "--listen", "unix://",
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
    # tmux owns window "foo" whose pane pid is 111 -> managed; pid 222 is only
    # in /proc -> external.
    monkeypatch.setattr(rc, "_tmux_window_pids", lambda: {"foo": 111})
    monkeypatch.setattr(rc, "_tmux_pane_alive", lambda target: True)
    monkeypatch.setattr(rc, "_capture_env_id", lambda target: "")
    monkeypatch.setattr(
        rc.proc, "scan_rc_servers",
        lambda: [ProcRC(111, "ws/foo", "/a"), ProcRC(222, "ws/bar", "/b")],
    )

    servers = rc.scan_servers()
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
    # managed, falling back to the window name, status from pane_alive.
    monkeypatch.setattr(rc, "_tmux_window_pids", lambda: {"foo": 111})
    monkeypatch.setattr(rc, "_tmux_pane_alive", lambda target: False)
    monkeypatch.setattr(rc, "_capture_env_id", lambda target: "")
    monkeypatch.setattr(rc.proc, "scan_rc_servers", lambda: [])

    servers = rc.scan_servers()
    assert len(servers) == 1
    assert servers[0].managed is True
    assert servers[0].name == "foo"
    assert servers[0].status == "dead"


# --- env_* capture pushed one-way into the ledger --------------------------

def test_scan_servers_captures_env_id_into_ledger(monkeypatch):
    captured: list[list[EnvRecord]] = []
    monkeypatch.setattr(rc, "_tmux_window_pids", lambda: {"foo": 111})
    monkeypatch.setattr(rc, "_tmux_pane_alive", lambda target: True)
    monkeypatch.setattr(
        rc, "_tmux_capture_pane",
        lambda target: "starting...\nenvironment=env_abc123XYZ\nready",
    )
    monkeypatch.setattr(rc.proc, "scan_rc_servers", lambda: [ProcRC(111, "ws/foo", "/a")])
    monkeypatch.setattr(rc.environments, "upsert", lambda recs: captured.append(recs))

    servers = rc.scan_servers()

    assert servers[0].env_id == "env_abc123XYZ"
    assert len(captured) == 1
    rec = captured[0][0]
    assert rec.prefix == "env"
    assert rec.key == "abc123XYZ"        # suffix only — env_id property reconstructs
    assert rec.bound_sid is None


def test_scan_servers_no_env_id_no_upsert(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(rc, "_tmux_window_pids", lambda: {"foo": 111})
    monkeypatch.setattr(rc, "_tmux_pane_alive", lambda target: True)
    monkeypatch.setattr(rc, "_tmux_capture_pane", lambda target: "no env here")
    monkeypatch.setattr(rc.proc, "scan_rc_servers", lambda: [ProcRC(111, "ws/foo", "/a")])
    monkeypatch.setattr(rc.environments, "upsert", lambda recs: calls.append(recs))

    servers = rc.scan_servers()
    assert servers[0].env_id is None
    assert calls == []  # no env captured -> ledger untouched


# --- remoteControlSpawnMode read (AC8 read half) ---------------------------

def _write_claude_json(tmp_path, projects):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"projects": projects}))
    return p


def test_read_spawn_mode_from_claude_json(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    cj = _write_claude_json(tmp_path, {
        str(ws / "proj"): {"hasTrustDialogAccepted": True,
                           "remoteControlSpawnMode": "same-dir"},
        str(ws / "other"): {"hasTrustDialogAccepted": True},
    })
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    monkeypatch.setattr(rc.cfg, "workspace", ws)

    assert rc._read_spawn_mode("proj") == "same-dir"
    assert rc._read_spawn_mode("other") is None      # key present, mode unset
    assert rc._read_spawn_mode("missing") is None     # key absent entirely


def test_scan_populates_spawn_mode(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    (ws / "proj").mkdir(parents=True)
    cj = _write_claude_json(tmp_path, {
        str(ws / "proj"): {"hasTrustDialogAccepted": True,
                           "remoteControlSpawnMode": "new-window"},
    })
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    monkeypatch.setattr(rc.cfg, "workspace", ws)
    monkeypatch.setattr(rc, "list_enabled", lambda: [])
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [])

    rows = {p.name: p for p in rc.scan()}
    assert rows["proj"].spawn_mode == "new-window"
