"""Tests for data/snapshot.py — the shared world + R6 ledger persistence.

`build_world_snapshot` runs on the worker thread once per cycle (R11/D8) and is
also the persistence point for the bridge-environment ledger (R6): it records
EVERY file-referenced env so a later toggle-away surfaces as an orphan. These
tests monkeypatch the data sources and point the ledger at a tmp dir.
"""

import json

from cc_session_control.config import cfg
from cc_session_control.data import environments as env
from cc_session_control.data import snapshot
from cc_session_control.models import SessionProc


def _sp(pid, sid, bridge=None, proc_start="1"):
    return SessionProc(pid=pid, sid=sid, bridge=bridge, proc_start=proc_start)


def _stub_sources(monkeypatch, procs):
    monkeypatch.setattr(snapshot.registry, "read_session_procs", lambda *a, **k: procs)
    monkeypatch.setattr(snapshot.registry, "read_agent_jobs", lambda *a, **k: [])
    monkeypatch.setattr(snapshot.sessions, "scan", lambda: [])
    monkeypatch.setattr(snapshot.rc, "scan", lambda: [])
    monkeypatch.setattr(snapshot.rc, "scan_servers", lambda: [])


def _ledger_keys(tmp_path):
    text = (tmp_path / "environments.jsonl").read_text()
    return {
        (json.loads(line)["prefix"], json.loads(line)["key"])
        for line in text.splitlines()
        if line.strip()
    }


def test_snapshot_persists_file_referenced_keeps_active_alive_gated(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)  # tmp ledger
    procs = [
        _sp(1, "sid-alive", bridge="session_ALIVE"),
        _sp(2, "sid-zombie", bridge="session_ZOMBIE"),
    ]
    _stub_sources(monkeypatch, procs)
    monkeypatch.setattr(snapshot.proc, "pid_alive", lambda pid, ps: pid == 1)

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
    monkeypatch.setattr(snapshot.proc, "pid_alive", lambda pid, ps: True)

    # Cycle 1: a session references env X.
    _stub_sources(monkeypatch, [_sp(1, "sid-x", bridge="session_X")])
    snapshot.build_world_snapshot()
    assert ("session", "X") in _ledger_keys(tmp_path)

    # Cycle 2: the session toggled RC off -> no file references X anymore.
    _stub_sources(monkeypatch, [_sp(1, "sid-x", bridge=None)])
    snap2 = snapshot.build_world_snapshot()

    assert snap2.file_referenced_envs == []
    orphans = env.orphan_envs(snap2.file_referenced_envs)
    assert any(e.env_id == "session_X" for e in orphans)
    manual = env.manual_delete_list(snap2.file_referenced_envs)
    assert any(r["env_id"] == "session_X" for r in manual)


def test_snapshot_reobserve_keeps_single_stable_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    monkeypatch.setattr(snapshot.proc, "pid_alive", lambda pid, ps: True)
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
