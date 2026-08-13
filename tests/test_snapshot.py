"""Tests for data/snapshot.py — the shared world snapshot.

`build_world_snapshot` runs on the worker thread once per cycle (R11/D8).
These tests monkeypatch the data sources.
"""

import os

from cc_session_control.config import cfg
from cc_session_control.data import liveness, registry, snapshot
from cc_session_control.data.proc import ProcRCInventory
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from cc_session_control.data.refresh import RefreshFailure, build_refresh_result
from cc_session_control.data.tmux import TmuxWindow, WindowInventory
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
    monkeypatch.setattr(
        snapshot.sessions,
        "scan_result",
        lambda inputs=None: snapshot.sessions.SessionScanResult(),
    )
    monkeypatch.setattr(
        snapshot.rc,
        "scan_result",
        lambda *, window_inventory, sessions=(): snapshot.rc.RCScanResult(
            [],
            ProjectSettingsResult(ProjectSettingsState.MISSING, {}),
        ),
    )
    monkeypatch.setattr(
        snapshot.rc,
        "_tmux_window_inventory",
        lambda: WindowInventory(),
    )
    monkeypatch.setattr(
        snapshot.rc,
        "scan_servers_result",
        lambda *, window_inventory: snapshot.rc.RCServerScanResult(),
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

    The liveness side performs targeted pid checks; the RC side performs the one
    full `/proc` traversal.  Keep those costs distinct in the regression test.
    """
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
        return snapshot.sessions.SessionScanResult()

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
    monkeypatch.setattr(snapshot.sessions, "scan_result", scan_sessions)
    monkeypatch.setattr(
        snapshot.rc,
        "scan_result",
        lambda *, window_inventory, sessions=(): snapshot.rc.RCScanResult(
            [],
            ProjectSettingsResult(ProjectSettingsState.MISSING, {}),
        ),
    )
    monkeypatch.setattr(
        snapshot.rc,
        "_tmux_window_inventory",
        lambda: WindowInventory(),
    )

    def scan_rc_procs():
        calls["rc_proc_scan"] += 1
        return ProcRCInventory()

    monkeypatch.setattr(
        snapshot.rc.proc,
        "scan_rc_server_inventory",
        scan_rc_procs,
    )

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
    assert first.agent_jobs is first.liveness_snapshot.agent_jobs
    assert [sp.pid for sp in first.liveness_snapshot.session_procs] == [101]
    assert [sp.pid for sp in second.liveness_snapshot.session_procs] == [202]


def test_snapshot_reuses_one_window_inventory_for_project_and_server_joins(
    monkeypatch,
):
    inventory = WindowInventory((TmuxWindow("@1", "project", False, 101, "/project"),))
    reads = 0
    injected: list[WindowInventory] = []

    def read_windows() -> WindowInventory:
        nonlocal reads
        reads += 1
        return inventory

    def scan_projects(*, window_inventory, sessions=()):
        injected.append(window_inventory)
        return snapshot.rc.RCScanResult(
            [],
            ProjectSettingsResult(ProjectSettingsState.MISSING, {}),
        )

    def scan_servers(*, window_inventory):
        injected.append(window_inventory)
        return snapshot.rc.RCServerScanResult()

    monkeypatch.setattr(
        snapshot.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        snapshot.sessions,
        "scan_result",
        lambda _inputs: snapshot.sessions.SessionScanResult(),
    )
    monkeypatch.setattr(snapshot.rc, "_tmux_window_inventory", read_windows)
    monkeypatch.setattr(snapshot.rc, "scan_result", scan_projects)
    monkeypatch.setattr(snapshot.rc, "scan_servers_result", scan_servers)

    snapshot.build_world_snapshot()

    assert reads == 1
    assert injected == [inventory, inventory]
    assert injected[0] is injected[1]
