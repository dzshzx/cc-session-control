"""Tests for data/sessions.py — the unified, multi-source scan() (AC1).

scan() merges three liveness/identity sources (registry sessions/<pid>.json,
`claude agents --json`, jobs/*/state.json) and projects each transcript through
`live_index()`. These tests feed monkeypatched cfg paths + a fake `pid_alive`
(no real /proc) and assert source/liveness/current/rc-exposure/agent-link.
"""

import builtins
import json
from types import MappingProxyType

from cc_session_control.config import cfg
from cc_session_control.data import liveness, proc, registry
from cc_session_control.data import sessions as sessions_mod
from cc_session_control.models import AgentJob, SessionProc

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
        _write_transcript(
            projects,
            sid,
            [
                {"cwd": "/work/proj1"},
                {"type": "user", "message": {"content": f"prompt for {sid[:3]}"}},
            ],
        )

    sessions = tmp_path / "sessions"
    _write_json(
        sessions / "1001.json",
        {
            "pid": 1001,
            "sessionId": CLI_SID,
            "cwd": "/work/proj1",
            "kind": "interactive",
            "entrypoint": "cli",
            "status": "busy",
            "procStart": "100",
            "bridgeSessionId": "session_aaa",
        },
    )
    _write_json(
        sessions / "1002.json",
        {
            "pid": 1002,
            "sessionId": VSC_SID,
            "cwd": "/work/proj1",
            "kind": "interactive",
            "entrypoint": "claude-vscode",
            "status": "idle",
            "procStart": "200",
        },
    )
    _write_json(
        sessions / "1003.json",
        {
            "pid": 1003,
            "sessionId": SDK_SID,
            "cwd": "/work/proj1",
            "kind": "interactive",
            "entrypoint": "sdk-ts",
            "status": "idle",
            "procStart": "300",
            "bridgeSessionId": "session_bbb",
        },
    )
    _write_json(
        sessions / "1004.json",
        {
            "pid": 1004,
            "sessionId": BG_SID,
            "cwd": "/work/proj1",
            "kind": "bg",
            "entrypoint": "cli",
            "status": "busy",
            "procStart": "400",
        },
    )

    _write_json(
        tmp_path / "jobs" / BG_SID[:8] / "state.json",
        {
            "state": "running",
            "sessionId": BG_SID,
            "resumeSessionId": BG_SID,
            "backend": "daemon",
        },
    )

    # pid 1003 (sdk) is a zombie file: registry entry exists but proc is dead.
    alive_pids = {1001, 1002, 1004}
    monkeypatch.setattr(
        proc,
        "probe_pid",
        lambda pid, start: proc.PidProbe(pid, pid in alive_pids),
    )
    # No `claude agents --json` data — liveness comes from the registry join.
    monkeypatch.setattr(sessions_mod, "alive_map", lambda: {})
    # The cli session launched csctl -> it is the "current" one.
    monkeypatch.setattr(sessions_mod, "_ancestor_pids", lambda: {1001})
    # No real tmux in unit tests: default to "nothing resident" (tests that
    # exercise the residency injection override this).
    monkeypatch.setattr(
        sessions_mod.tmux,
        "residency_inventory",
        lambda _pids: sessions_mod.tmux.ResidencyInventory(),
    )


def test_scan_unifies_sources(tmp_path, monkeypatch):
    _setup_world(tmp_path, monkeypatch)

    result = sessions_mod.scan_result()
    assert result.complete is True
    rows = {s.sid: s for s in result.sessions}
    assert set(rows) == {CLI_SID, VSC_SID, SDK_SID, BG_SID}

    # source bucket spans all four entrypoints.
    assert {s.source for s in rows.values()} == {"cli", "vscode", "sdk", "bg"}

    cli = rows[CLI_SID]
    assert cli.source == "cli"
    assert cli.alive is True
    assert cli.current is True  # pid 1001 in ancestor set
    assert cli.rc_exposed is True  # bridge string AND alive
    assert cli.env_id == "session_aaa"
    assert cli.agent_short is None
    assert cli.status == "busy"
    assert cli.pid == 1001

    vsc = rows[VSC_SID]
    assert vsc.source == "vscode"
    assert vsc.alive is True
    assert vsc.current is False
    assert vsc.rc_exposed is False  # no bridge
    assert vsc.env_id is None

    sdk = rows[SDK_SID]
    assert sdk.source == "sdk"
    assert sdk.alive is False  # pid 1003 is a zombie file
    assert sdk.current is False
    assert sdk.rc_exposed is False  # bridge present but proc dead
    assert sdk.env_id is None
    assert sdk.bridge_or_sdk is True  # D9: source==sdk surfaces it

    bg = rows[BG_SID]
    assert bg.source == "bg"  # registry kind == bg
    assert bg.alive is True
    assert bg.current is False
    assert bg.agent_short == BG_SID[:8]  # linked job short
    assert bg.status == "busy"


def test_scan_injects_tmux_residency_for_alive_sessions(tmp_path, monkeypatch):
    # ADR-0001 badge data: alive session with a pane-hosted pid gets its
    # tmux_target backfilled from ONE batch residency call; alive session with
    # no hit stays None; dead session stays None (its pids never queried).
    _setup_world(tmp_path, monkeypatch)
    seen = {}

    def fake_residency(pids):
        seen["pids"] = set(pids)
        return sessions_mod.tmux.ResidencyInventory({1001: "proj1:2"})

    monkeypatch.setattr(sessions_mod.tmux, "residency_inventory", fake_residency)

    rows = {s.sid: s for s in sessions_mod.scan_result().sessions}

    assert rows[CLI_SID].tmux_target == "proj1:2"  # alive + pane hit
    assert rows[VSC_SID].tmux_target is None  # alive, bare terminal
    assert rows[SDK_SID].tmux_target is None  # dead: never resident
    assert 1003 not in seen["pids"]  # dead pid not queried
    assert {1001, 1002, 1004} <= seen["pids"]


def test_scan_marks_alive_sessions_when_tmux_residency_is_incomplete(
    tmp_path,
    monkeypatch,
):
    _setup_world(tmp_path, monkeypatch)
    issue = sessions_mod.tmux.ResidencyIssue(
        "tmux list-panes",
        None,
        "lost server connection",
    )
    monkeypatch.setattr(
        sessions_mod.tmux,
        "residency_inventory",
        lambda _pids: sessions_mod.tmux.ResidencyInventory(issues=(issue,)),
    )

    rows = {session.sid: session for session in sessions_mod.scan_result().sessions}

    assert rows[CLI_SID].tmux_inventory_complete is False
    assert "lost server connection" in rows[CLI_SID].tmux_inventory_detail
    assert rows[VSC_SID].tmux_inventory_complete is False
    assert rows[SDK_SID].tmux_inventory_complete is True


def test_scan_residency_covers_all_alive_pids_of_a_sid(tmp_path, monkeypatch):
    # Multi-pid (resume) session: ANY alive pid inside a pane makes it resident.
    _setup_world(tmp_path, monkeypatch)
    # Second registry file for CLI_SID (resume kept the sid, minted a new pid).
    _write_json(
        tmp_path / "sessions" / "1005.json",
        {
            "pid": 1005,
            "sessionId": CLI_SID,
            "cwd": "/work/proj1",
            "kind": "interactive",
            "entrypoint": "cli",
            "status": "idle",
            "procStart": "500",
        },
    )
    registry.invalidate_cache()
    alive_pids = {1001, 1002, 1004, 1005}
    monkeypatch.setattr(
        proc,
        "probe_pid",
        lambda pid, start: proc.PidProbe(pid, pid in alive_pids),
    )
    # Only the OLDER pid 1001 lives in a tmux pane.
    monkeypatch.setattr(
        sessions_mod.tmux,
        "residency_inventory",
        lambda pids: sessions_mod.tmux.ResidencyInventory(
            {1001: "proj1:3"} if 1001 in set(pids) else {}
        ),
    )

    rows = {s.sid: s for s in sessions_mod.scan_result().sessions}
    assert rows[CLI_SID].tmux_target == "proj1:3"


def test_scan_transcript_only_session_is_dead(tmp_path, monkeypatch):
    # A transcript with no registry/agents entry stays dead with empty source.
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    projects = tmp_path / "projects"
    _write_transcript(
        projects,
        "orphan-sid",
        [
            {"cwd": "/work/x"},
            {"type": "user", "message": {"content": "hello"}},
        ],
    )
    monkeypatch.setattr(
        proc,
        "probe_pid",
        lambda pid, start: proc.PidProbe(pid, False),
    )
    monkeypatch.setattr(sessions_mod, "alive_map", lambda: {})
    monkeypatch.setattr(sessions_mod, "_ancestor_pids", lambda: set())

    rows = {s.sid: s for s in sessions_mod.scan_result().sessions}
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
    _write_transcript(
        projects,
        "nocwd-sid",
        [
            {"type": "user", "message": {"content": "no cwd here"}},
        ],
    )
    monkeypatch.setattr(
        proc,
        "probe_pid",
        lambda pid, start: proc.PidProbe(pid, False),
    )
    monkeypatch.setattr(sessions_mod, "alive_map", lambda: {})
    monkeypatch.setattr(sessions_mod, "_ancestor_pids", lambda: set())

    result = sessions_mod.scan_result()

    assert result.complete is True
    assert result.sessions == ()


def test_scan_uses_injected_generation_liveness_without_reading_sources(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    sid = "injected-sid"
    _write_transcript(
        tmp_path / "projects",
        sid,
        [
            {"cwd": "/work/injected"},
            {"type": "user", "message": {"content": "hello"}},
        ],
    )
    inputs = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=5150,
                sid=sid,
                proc_start="1",
                proc_alive=True,
            ),
        ),
        cur=frozenset({5150}),
        agent_jobs=(
            AgentJob(
                short=sid[:8],
                sid=sid,
                resume_sid=sid,
                host_pid=5150,
                host_alive=True,
            ),
        ),
        agents_map=MappingProxyType({sid: 5150}),
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("injected scan must not re-read liveness sources")

    monkeypatch.setattr(sessions_mod, "live_session_procs", unexpected)
    monkeypatch.setattr(sessions_mod, "alive_map", unexpected)
    monkeypatch.setattr(sessions_mod.registry, "read_agent_jobs", unexpected)
    monkeypatch.setattr(sessions_mod, "_ancestor_pids", unexpected)
    monkeypatch.setattr(
        sessions_mod.tmux,
        "residency_inventory",
        lambda _pids: sessions_mod.tmux.ResidencyInventory(),
    )

    rows = sessions_mod.scan_result(inputs).sessions

    assert len(rows) == 1
    assert rows[0].sid == sid
    assert rows[0].alive
    assert rows[0].current
    assert rows[0].agent_short == sid[:8]


def test_scan_result_retains_stat_failure_as_incomplete_evidence(
    tmp_path,
    monkeypatch,
):
    _setup_world(tmp_path, monkeypatch)
    denied = tmp_path / "projects" / "proj1" / f"{CLI_SID}.jsonl"
    real_stat = sessions_mod.os.stat

    def stat(path):
        if path == str(denied):
            raise PermissionError("transcript stat denied")
        return real_stat(path)

    monkeypatch.setattr(sessions_mod.os, "stat", stat)

    result = sessions_mod.scan_result()

    assert result.complete is False
    assert {row.sid for row in result.sessions} == {VSC_SID, SDK_SID, BG_SID}
    assert len(result.issues) == 1
    assert result.issues[0].source == "session transcript"
    assert result.issues[0].path == str(denied)
    assert "transcript stat denied" in result.issues[0].detail


def test_scan_result_reports_project_directory_enumeration_failure(
    tmp_path,
    monkeypatch,
):
    _setup_world(tmp_path, monkeypatch)
    denied = tmp_path / "projects" / "proj1"
    real_scandir = sessions_mod.os.scandir

    def scandir(path):
        if sessions_mod.os.fspath(path) == str(denied):
            raise PermissionError("transcript directory denied")
        return real_scandir(path)

    monkeypatch.setattr(sessions_mod.os, "scandir", scandir)

    result = sessions_mod.scan_result()

    assert result.sessions == ()
    assert result.complete is False
    assert len(result.issues) == 1
    assert result.issues[0].source == "session transcript inventory"
    assert result.issues[0].path == str(denied)
    assert "transcript directory denied" in result.issues[0].detail


def test_scan_result_retains_open_failure_as_incomplete_evidence(
    tmp_path,
    monkeypatch,
):
    _setup_world(tmp_path, monkeypatch)
    denied = tmp_path / "projects" / "proj1" / f"{CLI_SID}.jsonl"
    real_open = builtins.open

    def open_file(path, *args, **kwargs):
        if sessions_mod.os.fspath(path) == str(denied):
            raise PermissionError("transcript open denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_file)

    result = sessions_mod.scan_result()

    assert result.complete is False
    assert {row.sid for row in result.sessions} == {VSC_SID, SDK_SID, BG_SID}
    assert result.issues[0].path == str(denied)
    assert "transcript open denied" in result.issues[0].detail


def test_scan_result_retains_mid_read_unicode_failure_as_incomplete_evidence(
    tmp_path,
    monkeypatch,
):
    _setup_world(tmp_path, monkeypatch)
    broken = tmp_path / "projects" / "proj1" / f"{CLI_SID}.jsonl"
    broken.write_bytes(b'{"cwd":"/work/proj1"}\n\xff\n')

    result = sessions_mod.scan_result()

    assert result.complete is False
    assert {row.sid for row in result.sessions} == {VSC_SID, SDK_SID, BG_SID}
    assert result.issues[0].path == str(broken)
    assert "codec can't decode byte" in result.issues[0].detail


def test_scan_result_ignores_malformed_lines_in_readable_transcript(
    tmp_path,
    monkeypatch,
):
    _setup_world(tmp_path, monkeypatch)
    transcript = tmp_path / "projects" / "proj1" / f"{CLI_SID}.jsonl"
    transcript.write_text('not json\n{"cwd":"/work/proj1"}\n', encoding="utf-8")

    result = sessions_mod.scan_result()

    assert result.complete is True
    assert CLI_SID in {row.sid for row in result.sessions}


def test_scan_result_retains_relevant_json_failure_as_incomplete_evidence(
    tmp_path,
    monkeypatch,
):
    _setup_world(tmp_path, monkeypatch)
    broken = tmp_path / "projects" / "proj1" / f"{CLI_SID}.jsonl"
    broken.write_text('{"cwd":"/work/proj1"\n', encoding="utf-8")

    result = sessions_mod.scan_result()

    assert result.complete is False
    assert {row.sid for row in result.sessions} == {VSC_SID, SDK_SID, BG_SID}
    assert result.issues[0].source == "session transcript"
    assert result.issues[0].path == str(broken)
    assert "line 1: invalid JSON" in result.issues[0].detail


def test_scan_result_missing_projects_root_is_complete_empty_state(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)

    result = sessions_mod.scan_result(liveness.LivenessSnapshot())

    assert result.complete is True
    assert result.sessions == ()
    assert result.issues == ()


def test_scan_result_exposes_pathname_only_sids(tmp_path, monkeypatch):
    """F47: an empty transcript yields no session row but its sid stays discoverable."""
    _setup_world(tmp_path, monkeypatch)
    (tmp_path / "projects" / "proj1" / "path-only-sid.jsonl").write_text("")

    result = sessions_mod.scan_result()

    assert "path-only-sid" not in {row.sid for row in result.sessions}
    assert "path-only-sid" in result.path_sids
    assert "path-only-sid" in result.sids
    assert {row.sid for row in result.sessions} <= result.sids
