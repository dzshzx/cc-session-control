"""Tests for data/sessions.py — the unified, multi-source scan() (AC1).

scan() merges three liveness/identity sources (registry sessions/<pid>.json,
`claude agents --json`, jobs/*/state.json) and projects each transcript through
`live_index()`. These tests feed monkeypatched cfg paths + a fake `pid_alive`
(no real /proc) and assert source/liveness/current/rc-exposure/agent-link.
"""

import json

from cc_session_control.config import cfg
from cc_session_control.data import registry
from cc_session_control.data import sessions as sessions_mod

CLI_SID = "cli11111-1111-1111-1111-111111111111"
VSC_SID = "vsc22222-2222-2222-2222-222222222222"
SDK_SID = "sdk33333-3333-3333-3333-333333333333"
BG_SID = "bgaa4444-4444-4444-4444-444444444444"


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _write_transcript(projects, sid, lines):
    f = projects / "proj1" / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n"
    )
    return str(f)


def _setup_world(tmp_path, monkeypatch):
    """Lay down transcripts + registry fixtures for a 4-source world."""
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()

    projects = tmp_path / "projects"
    for sid in (CLI_SID, VSC_SID, SDK_SID, BG_SID):
        _write_transcript(projects, sid, [
            {"cwd": "/work/proj1"},
            {"type": "user", "message": {"content": f"prompt for {sid[:3]}"}},
        ])

    sessions = tmp_path / "sessions"
    _write_json(sessions / "1001.json", {
        "pid": 1001, "sessionId": CLI_SID, "cwd": "/work/proj1",
        "kind": "interactive", "entrypoint": "cli", "status": "busy",
        "procStart": "100", "bridgeSessionId": "session_aaa",
    })
    _write_json(sessions / "1002.json", {
        "pid": 1002, "sessionId": VSC_SID, "cwd": "/work/proj1",
        "kind": "interactive", "entrypoint": "claude-vscode", "status": "idle",
        "procStart": "200",
    })
    _write_json(sessions / "1003.json", {
        "pid": 1003, "sessionId": SDK_SID, "cwd": "/work/proj1",
        "kind": "interactive", "entrypoint": "sdk-ts", "status": "idle",
        "procStart": "300", "bridgeSessionId": "session_bbb",
    })
    _write_json(sessions / "1004.json", {
        "pid": 1004, "sessionId": BG_SID, "cwd": "/work/proj1",
        "kind": "bg", "entrypoint": "cli", "status": "busy",
        "procStart": "400",
    })

    _write_json(tmp_path / "jobs" / BG_SID[:8] / "state.json", {
        "state": "running", "sessionId": BG_SID, "resumeSessionId": BG_SID,
        "backend": "daemon",
    })

    # pid 1003 (sdk) is a zombie file: registry entry exists but proc is dead.
    alive_pids = {1001, 1002, 1004}
    monkeypatch.setattr(sessions_mod, "pid_alive", lambda pid, ps: pid in alive_pids)
    # No `claude agents --json` data — liveness comes from the registry join.
    monkeypatch.setattr(sessions_mod, "alive_map", lambda: {})
    # The cli session launched csctl -> it is the "current" one.
    monkeypatch.setattr(sessions_mod, "_ancestor_pids", lambda: {1001})


def test_scan_unifies_sources(tmp_path, monkeypatch):
    _setup_world(tmp_path, monkeypatch)

    rows = {s.sid: s for s in sessions_mod.scan()}
    assert set(rows) == {CLI_SID, VSC_SID, SDK_SID, BG_SID}

    # source bucket spans all four entrypoints.
    assert {s.source for s in rows.values()} == {"cli", "vscode", "sdk", "bg"}

    cli = rows[CLI_SID]
    assert cli.source == "cli"
    assert cli.alive is True
    assert cli.current is True            # pid 1001 in ancestor set
    assert cli.rc_exposed is True         # bridge string AND alive
    assert cli.env_id == "session_aaa"
    assert cli.agent_short is None
    assert cli.status == "busy"
    assert cli.pid == 1001

    vsc = rows[VSC_SID]
    assert vsc.source == "vscode"
    assert vsc.alive is True
    assert vsc.current is False
    assert vsc.rc_exposed is False        # no bridge
    assert vsc.env_id is None

    sdk = rows[SDK_SID]
    assert sdk.source == "sdk"
    assert sdk.alive is False             # pid 1003 is a zombie file
    assert sdk.current is False
    assert sdk.rc_exposed is False        # bridge present but proc dead
    assert sdk.env_id is None
    assert sdk.bridge_or_sdk is True      # D9: source==sdk surfaces it

    bg = rows[BG_SID]
    assert bg.source == "bg"              # registry kind == bg
    assert bg.alive is True
    assert bg.current is False
    assert bg.agent_short == BG_SID[:8]   # linked job short
    assert bg.status == "busy"


def test_scan_transcript_only_session_is_dead(tmp_path, monkeypatch):
    # A transcript with no registry/agents entry stays dead with empty source.
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    projects = tmp_path / "projects"
    _write_transcript(projects, "orphan-sid", [
        {"cwd": "/work/x"},
        {"type": "user", "message": {"content": "hello"}},
    ])
    monkeypatch.setattr(sessions_mod, "pid_alive", lambda pid, ps: False)
    monkeypatch.setattr(sessions_mod, "alive_map", lambda: {})
    monkeypatch.setattr(sessions_mod, "_ancestor_pids", lambda: set())

    rows = {s.sid: s for s in sessions_mod.scan()}
    s = rows["orphan-sid"]
    assert s.alive is False
    assert s.current is False
    assert s.source == ""
    assert s.rc_exposed is False
    assert s.env_id is None
    assert s.agent_short is None


def test_scan_excludes_transcript_without_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    projects = tmp_path / "projects"
    _write_transcript(projects, "nocwd-sid", [
        {"type": "user", "message": {"content": "no cwd here"}},
    ])
    monkeypatch.setattr(sessions_mod, "pid_alive", lambda pid, ps: False)
    monkeypatch.setattr(sessions_mod, "alive_map", lambda: {})
    monkeypatch.setattr(sessions_mod, "_ancestor_pids", lambda: set())

    assert sessions_mod.scan() == []
