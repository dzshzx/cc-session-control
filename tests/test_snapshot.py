"""Tests for data/snapshot.py — the shared world + R6 ledger persistence.

`build_world_snapshot` runs on the worker thread once per cycle (R11/D8) and is
also the persistence point for the bridge-environment ledger (R6): it records
EVERY file-referenced env so a later toggle-away surfaces as an orphan. These
tests monkeypatch the data sources and point the ledger at a tmp dir.
"""

import json
from pathlib import Path

from cc_session_control.config import cfg
from cc_session_control.data import environment_ledger as ledger
from cc_session_control.data import environments as env
from cc_session_control.data import liveness, registry, snapshot
from cc_session_control.data.proc import ProcRC
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from cc_session_control.data.rc_environment import EnvironmentIdCache
from cc_session_control.data.refresh import RefreshFailure, build_refresh_result
from cc_session_control.data.tmux import TmuxWindow
from cc_session_control.models import SessionProc


def _sp(pid, sid, bridge=None, proc_start="1"):
    return SessionProc(pid=pid, sid=sid, bridge=bridge, proc_start=proc_start)


def _stub_sources(monkeypatch, procs):
    monkeypatch.setattr(
        snapshot.liveness.registry,
        "scan_session_procs",
        lambda *a, **k: registry.RegistryScan(records=tuple(procs)),
    )
    monkeypatch.setattr(
        snapshot.liveness.registry,
        "scan_agent_jobs",
        lambda *a, **k: registry.RegistryScan(),
    )
    monkeypatch.setattr(
        snapshot.liveness,
        "scan_agents",
        lambda *a, **k: liveness.AgentsScan(),
    )
    monkeypatch.setattr(snapshot.sessions, "scan", lambda inputs=None: [])
    monkeypatch.setattr(
        snapshot.rc,
        "scan_result",
        lambda: snapshot.rc.RCScanResult(
            [],
            ProjectSettingsResult(ProjectSettingsState.MISSING, {}),
        ),
    )
    monkeypatch.setattr(snapshot.rc, "scan_servers", lambda: [])


def _ledger_keys(tmp_path):
    text = (tmp_path / "environments.jsonl").read_text()
    return {
        (json.loads(line)["prefix"], json.loads(line)["key"])
        for line in text.splitlines()
        if line.strip()
    }


def test_snapshot_persists_file_referenced_keeps_active_alive_gated(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)  # tmp ledger
    procs = [
        _sp(1, "sid-alive", bridge="session_ALIVE"),
        _sp(2, "sid-zombie", bridge="session_ZOMBIE"),
    ]
    _stub_sources(monkeypatch, procs)
    monkeypatch.setattr(
        snapshot.liveness.proc,
        "probe_pid",
        lambda pid, start: snapshot.liveness.proc.PidProbe(pid, pid == 1),
    )

    snap = snapshot.build_world_snapshot()

    # file-referenced carries BOTH bridges (membership, not liveness)...
    fr = {(r.prefix, r.key) for r in snap.file_referenced_envs}
    assert fr == {("session", "ALIVE"), ("session", "ZOMBIE")}
    # ...and the ledger persisted both, so the zombie can orphan later.
    assert _ledger_keys(tmp_path) == {("session", "ALIVE"), ("session", "ZOMBIE")}

    # observed (alive-gated) excludes the zombie -> active display stays honest.
    obs = {(r.prefix, r.key) for r in snap.observed_envs}
    assert obs == {("session", "ALIVE")}
    current = env.current_envs(snap.observed_envs)
    assert all(e.env_id != "session_ZOMBIE" for e in current)


def test_snapshot_toggle_away_becomes_orphan(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    monkeypatch.setattr(
        snapshot.liveness.proc,
        "probe_pid",
        lambda pid, start: snapshot.liveness.proc.PidProbe(pid, True),
    )

    # Cycle 1: a session references env X.
    _stub_sources(monkeypatch, [_sp(1, "sid-x", bridge="session_X")])
    snapshot.build_world_snapshot()
    assert ("session", "X") in _ledger_keys(tmp_path)

    # Cycle 2: the session toggled RC off -> no file references X anymore.
    _stub_sources(monkeypatch, [_sp(1, "sid-x", bridge=None)])
    snap2 = snapshot.build_world_snapshot()

    assert snap2.file_referenced_envs == ()
    orphans = env.orphan_envs(snap2.file_referenced_envs)
    assert any(e.env_id == "session_X" for e in orphans)


def test_snapshot_reobserve_keeps_single_stable_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    monkeypatch.setattr(
        snapshot.liveness.proc,
        "probe_pid",
        lambda pid, start: snapshot.liveness.proc.PidProbe(pid, True),
    )
    _stub_sources(monkeypatch, [_sp(1, "sid-x", bridge="session_X")])

    snapshot.build_world_snapshot()
    path = tmp_path / "environments.jsonl"
    assert len(path.read_text().splitlines()) == 1

    # Same world again: a re-observed env stays ONE entry (no duplication), and
    # membership is stable (write-on-change itself is unit-tested with an injected
    # `now` in test_environments.py; here `now` is real time so last_seen advances).
    snapshot.build_world_snapshot()
    assert _ledger_keys(tmp_path) == {("session", "X")}
    assert len(path.read_text().splitlines()) == 1


def test_snapshot_keeps_current_environment_and_carries_ledger_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    path = cfg.environments_ledger
    path.write_text('{"prefix":"session","key":"OLD"}\n')
    original_open = Path.open

    def deny_ledger(target, *args, **kwargs):
        if target == path:
            raise PermissionError("snapshot history denied")
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_ledger)
    _stub_sources(monkeypatch, [_sp(1, "sid-live", bridge="session_LIVE")])
    monkeypatch.setattr(
        snapshot.liveness.proc,
        "probe_pid",
        lambda pid, start: snapshot.liveness.proc.PidProbe(pid, True),
    )

    snap = snapshot.build_world_snapshot()

    assert [item.env_id for item in snap.environment_reconciliation.current] == [
        "session_LIVE",
    ]
    assert not snap.environment_reconciliation.ledger_history_complete
    assert any(
        "snapshot history denied" in warning
        for warning in snap.environment_reconciliation.warnings
    )


def test_incomplete_snapshot_fails_without_mutating_environment_ledger(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    env.upsert([env.EnvRecord("session", "OLD", "sid-old")], now=1.0)
    original = cfg.environments_ledger.read_bytes()
    _stub_sources(monkeypatch, [])
    evidence = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=1,
                sid="sid-live",
                bridge="session_PARTIAL",
                proc_alive=True,
            ),
        ),
        issues=(
            liveness.LivenessIssue(
                "job registry",
                "/runtime/jobs/broken/state.json",
                "invalid JSON",
            ),
        ),
    )
    monkeypatch.setattr(snapshot.liveness, "liveness_inputs", lambda: evidence)

    result = build_refresh_result(
        8,
        snapshot_builder=snapshot.build_world_snapshot,
    )

    assert isinstance(result, RefreshFailure)
    assert "job registry" in result.source
    assert cfg.environments_ledger.read_bytes() == original


def test_snapshot_reconciliation_owns_single_rc_environment_ledger_update(
    monkeypatch,
):
    actual_scan_servers = snapshot.rc.scan_servers
    _stub_sources(monkeypatch, [])
    monkeypatch.setattr(snapshot.rc, "scan_servers", actual_scan_servers)
    monkeypatch.setattr(
        snapshot.rc,
        "_tmux_windows",
        lambda: [TmuxWindow("@1", "foo", False, 111, "/a")],
    )
    monkeypatch.setattr(
        snapshot.rc,
        "_tmux_capture_pane",
        lambda target: "environment=env_CAPTURED",
    )
    monkeypatch.setattr(
        snapshot.rc.proc,
        "scan_rc_servers",
        lambda: [ProcRC(111, "ws/foo", "/a")],
    )
    monkeypatch.setattr(snapshot.rc, "_environment_ids", EnvironmentIdCache())

    failed = ledger.LedgerUpdate(
        ledger.LedgerUpdateState.FAILED,
        read=ledger.LedgerRead(ledger.LedgerReadState.MISSING),
        failure=ledger.LedgerFailure.WRITE,
        detail="first write failed",
    )
    later_success = ledger.LedgerUpdate(
        ledger.LedgerUpdateState.WRITTEN,
        read=ledger.LedgerRead(ledger.LedgerReadState.MISSING),
    )
    outcomes = iter((failed, later_success))
    updates = []

    def record_update(records, now=None):
        updates.append(records)
        return next(outcomes)

    monkeypatch.setattr(env, "upsert", record_update)

    snap = snapshot.build_world_snapshot()

    assert [
        [(record.prefix, record.key) for record in records] for records in updates
    ] == [[("env", "CAPTURED")]]
    assert snap.rc_servers[0].env_id == "env_CAPTURED"
    assert [(record.prefix, record.key) for record in snap.file_referenced_envs] == [
        ("env", "CAPTURED")
    ]
    assert snap.environment_reconciliation.ledger is failed
    assert any(
        "first write failed" in warning
        for warning in snap.environment_reconciliation.warnings
    )


def test_snapshot_captures_each_liveness_source_once_per_generation(
    tmp_path,
    monkeypatch,
):
    """Sessions consumes the generation's injected liveness, not fresh proc reads.

    The liveness side performs targeted pid checks; the RC side performs the one
    full `/proc` traversal.  Keep those costs distinct in the regression test.
    """
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    calls = {
        "registry_sessions": 0,
        "pid_alive": 0,
        "jobs": 0,
        "agents": 0,
        "ancestors": 0,
        "rc_proc_scan": 0,
    }
    generations = iter((101, 202))
    active_pid = {"value": next(generations)}

    def read_session_procs(*args, **kwargs):
        calls["registry_sessions"] += 1
        return registry.RegistryScan(
            records=(_sp(active_pid["value"], f"sid-{active_pid['value']}"),)
        )

    def probe_pid(pid, proc_start):
        calls["pid_alive"] += 1
        return snapshot.liveness.proc.PidProbe(pid, True)

    def read_jobs(*args, **kwargs):
        calls["jobs"] += 1
        return registry.RegistryScan()

    def read_agents(*args, **kwargs):
        calls["agents"] += 1
        return liveness.AgentsScan()

    def read_ancestors():
        calls["ancestors"] += 1
        return snapshot.liveness.proc.AncestorProbe(frozenset({active_pid["value"]}))

    injected = []

    def scan_sessions(inputs=None):
        injected.append(inputs)
        return []

    monkeypatch.setattr(
        snapshot.liveness.registry, "scan_session_procs", read_session_procs
    )
    monkeypatch.setattr(snapshot.liveness.proc, "probe_pid", probe_pid)
    monkeypatch.setattr(snapshot.liveness.registry, "scan_agent_jobs", read_jobs)
    monkeypatch.setattr(snapshot.liveness, "scan_agents", read_agents)
    monkeypatch.setattr(
        snapshot.liveness.proc,
        "probe_current_ancestors",
        read_ancestors,
    )
    monkeypatch.setattr(snapshot.sessions, "scan", scan_sessions)
    monkeypatch.setattr(
        snapshot.rc,
        "scan_result",
        lambda: snapshot.rc.RCScanResult(
            [],
            ProjectSettingsResult(ProjectSettingsState.MISSING, {}),
        ),
    )
    monkeypatch.setattr(snapshot.rc, "_tmux_windows", lambda: [])

    def scan_rc_procs():
        calls["rc_proc_scan"] += 1
        return []

    monkeypatch.setattr(snapshot.rc.proc, "scan_rc_servers", scan_rc_procs)

    first = snapshot.build_world_snapshot()
    active_pid["value"] = next(generations)
    second = snapshot.build_world_snapshot()

    assert calls == {
        "registry_sessions": 2,
        "pid_alive": 2,
        "jobs": 2,
        "agents": 2,
        "ancestors": 2,
        "rc_proc_scan": 2,
    }
    assert injected == [first.liveness_snapshot, second.liveness_snapshot]
    assert first.liveness_snapshot is not second.liveness_snapshot
    assert first.session_procs is first.liveness_snapshot.session_procs
    assert first.agent_jobs is first.liveness_snapshot.agent_jobs
    assert first.agents_map == first.liveness_snapshot.agents_map
    assert first.agents_map is not first.liveness_snapshot.agents_map
    assert first.cur is first.liveness_snapshot.cur
    assert [sp.pid for sp in first.session_procs] == [101]
    assert [sp.pid for sp in second.session_procs] == [202]
