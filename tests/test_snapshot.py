"""Tests for data/snapshot.py — the shared world snapshot.

`build_world_snapshot` runs on the worker thread once per cycle (R11/D8).
These tests monkeypatch the data sources.
"""

import os

from cc_session_control.config import cfg
from cc_session_control.data import liveness, membership, registry, snapshot
from cc_session_control.data.refresh import RefreshFailure, build_refresh_result
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
        snapshot.liveness,
        "scan_agents",
        lambda *a, **k: liveness.AgentsScan(),
    )
    monkeypatch.setattr(
        snapshot.sessions,
        "scan_result",
        lambda inputs=None: snapshot.sessions.SessionScanResult(),
    )
    monkeypatch.setattr(
        snapshot.membership,
        "scan_projects",
        lambda sessions=(): membership.ProjectsScan(),
    )


def test_incomplete_liveness_snapshot_fails_generation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
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
                "session registry",
                "/runtime/sessions/broken.json",
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
    assert "session registry" in result.source


def test_incomplete_transcript_snapshot_fails_before_cleanup_plan(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    aged = cfg.shell_snapshots_dir / "old.sh"
    aged.parent.mkdir(parents=True)
    aged.touch()
    os.utime(aged, (0.0, 0.0))
    _stub_sources(monkeypatch, [])
    issue = snapshot.sessions.TranscriptIssue(
        "session transcript",
        "/runtime/projects/project/session.jsonl",
        "permission denied",
    )
    monkeypatch.setattr(
        snapshot.sessions,
        "scan_result",
        lambda inputs: snapshot.sessions.SessionScanResult(issues=(issue,)),
    )

    result = build_refresh_result(
        9,
        snapshot_builder=snapshot.build_world_snapshot,
        cleanup_builder=lambda *_args: (_ for _ in ()).throw(
            AssertionError("partial transcript evidence must not build a cleanup plan")
        ),
    )

    assert isinstance(result, RefreshFailure)
    assert result.generation == 9
    assert result.source == (
        "session transcript (/runtime/projects/project/session.jsonl)"
    )
    assert result.detail == (
        "session transcript (/runtime/projects/project/session.jsonl): "
        "permission denied"
    )
    assert result.cleanup_plan.aged_entries == ("shell-snapshots/old.sh",)
    assert set(result.cleanup_plan.aged_anchors) == {"shell-snapshots/old.sh"}
    assert result.cleanup_plan.empty == ()
    assert result.cleanup_plan.short == ()
    assert result.cleanup_plan.orphan_entries == ()
    assert result.cleanup_plan.zombie_pids == ()


def test_snapshot_captures_each_liveness_source_once_per_generation(
    monkeypatch,
):
    """Sessions consumes the generation's injected liveness, not fresh proc reads.

    The liveness side performs targeted pid checks — one registry read, one
    agents probe, one ancestor walk per generation, never a second pass.
    """
    calls = {
        "registry_sessions": 0,
        "pid_alive": 0,
        "agents": 0,
        "ancestors": 0,
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

    def read_agents(*args, **kwargs):
        calls["agents"] += 1
        return liveness.AgentsScan()

    def read_ancestors():
        calls["ancestors"] += 1
        return snapshot.liveness.proc.AncestorProbe(frozenset({active_pid["value"]}))

    injected = []

    def scan_sessions(inputs=None):
        injected.append(inputs)
        return snapshot.sessions.SessionScanResult()

    monkeypatch.setattr(
        snapshot.liveness.registry, "scan_session_procs", read_session_procs
    )
    monkeypatch.setattr(snapshot.liveness.proc, "probe_pid", probe_pid)
    monkeypatch.setattr(snapshot.liveness, "scan_agents", read_agents)
    monkeypatch.setattr(
        snapshot.liveness.proc,
        "probe_current_ancestors",
        read_ancestors,
    )
    monkeypatch.setattr(snapshot.sessions, "scan_result", scan_sessions)
    monkeypatch.setattr(
        snapshot.membership,
        "scan_projects",
        lambda sessions=(): membership.ProjectsScan(),
    )

    first = snapshot.build_world_snapshot()
    active_pid["value"] = next(generations)
    second = snapshot.build_world_snapshot()

    assert calls == {
        "registry_sessions": 2,
        "pid_alive": 2,
        "agents": 2,
        "ancestors": 2,
    }
    assert injected == [first.liveness_snapshot, second.liveness_snapshot]
    assert first.liveness_snapshot is not second.liveness_snapshot
    assert [sp.pid for sp in first.liveness_snapshot.session_procs] == [101]
    assert [sp.pid for sp in second.liveness_snapshot.session_procs] == [202]
