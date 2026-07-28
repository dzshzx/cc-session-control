"""Tests for data/liveness.py — live_index purity and the alive_map cache."""

import dataclasses
import json
import subprocess

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import liveness, registry
from cc_session_control.models import AgentJob, SessionProc


def _sp(sid, pid, proc_start, proc_alive=False, **kw):
    return SessionProc(
        pid=pid, sid=sid, proc_start=proc_start, proc_alive=proc_alive, **kw
    )


# --- live_index: pure merge, AC2 matrix ---


def test_live_index_zombie_file_not_alive():
    # A sessions/*.json whose pid is dead (no /proc) and not in agents_map.
    idx = liveness.live_index([_sp("dead", 4242, "123")], {})
    assert idx["dead"].alive is False
    assert idx["dead"].pid is None
    assert idx["dead"].proc_alive is False


def test_live_index_procstart_mismatch_is_dead():
    # pid exists but starttime mismatches (reuse) -> injected proc_alive False.
    idx = liveness.live_index([_sp("reuse", 700, "OLD")], {})
    assert idx["reuse"].alive is False


def test_live_index_same_sid_multiple_pids_picks_alive():
    # resume keeps sid, mints new pid: 700772 dead, 710575 alive.
    procs = [
        _sp("f1f71921", 700772, "100", status="idle"),
        _sp("f1f71921", 710575, "200", proc_alive=True, status="busy"),
    ]
    idx = liveness.live_index(procs, {})
    info = idx["f1f71921"]
    assert info.alive is True
    assert info.pid == 710575
    assert info.proc_start == "200"
    assert info.status == "busy"
    assert info.proc_alive is True


def test_live_index_picks_newest_when_several_alive():
    procs = [
        _sp("sid", 1, "100", proc_alive=True),
        _sp("sid", 2, "300", proc_alive=True),  # newest procStart
        _sp("sid", 3, "200", proc_alive=True),
    ]
    idx = liveness.live_index(procs, {})
    assert idx["sid"].pid == 2
    assert idx["sid"].proc_start == "300"


def test_live_index_records_all_alive_pids():
    # Flag ① — `pids` must list every alive pid, not just the chosen newest, so
    # "current" detection can protect a resumed sid via any ancestor pid.
    procs = [
        _sp("sid", 700772, "100", proc_alive=True),  # older
        _sp("sid", 710575, "200", proc_alive=True),  # newer -> chosen pid
        _sp("sid", 700001, "150"),  # dead -> excluded from pids
    ]
    info = liveness.live_index(procs, {})["sid"]
    assert info.pid == 710575
    assert set(info.pids) == {700772, 710575}


def test_live_index_dead_sid_has_no_pids():
    info = liveness.live_index([_sp("dead", 4242, "123")], {})["dead"]
    assert info.alive is False
    assert info.pids == []


def test_live_index_agent_only_records_pid():
    info = liveness.live_index([], {"agentsid": 9001})["agentsid"]
    assert info.pids == [9001]


def test_live_index_degrades_to_agents_map():
    # Non-Linux: proc_alive False, but agents_map says the sid is alive.
    procs = [_sp("sid", 4242, "123")]
    idx = liveness.live_index(procs, {"sid": 5555})
    info = idx["sid"]
    assert info.alive is True
    assert info.pid == 5555  # taken from agents_map since proc pid is unverified
    assert info.proc_alive is False


def test_live_index_agent_only_sid():
    # A sid present only in agents_map (no sessions/*.json) still appears.
    idx = liveness.live_index([], {"agentsid": 9001})
    assert idx["agentsid"].alive is True
    assert idx["agentsid"].pid == 9001
    assert idx["agentsid"].proc_alive is False


def test_live_index_pidless_agents_entry_not_alive():
    # `claude agents --json` keeps listing settled/blocked bg sessions but with
    # NO pid — those are not alive (nothing to signal; terminate would always
    # fail). Judged by pid non-empty, per the session-doctor contract.
    dead_proc = _sp("bgsid", 629638, "123", kind="bg")
    idx = liveness.live_index([dead_proc], {"bgsid": None})
    assert idx["bgsid"].alive is False
    assert idx["bgsid"].pid is None


def test_live_index_pidless_agent_only_sid_not_alive():
    # Same, without any sessions/*.json backing the sid.
    idx = liveness.live_index([], {"bgsid": None})
    assert idx["bgsid"].alive is False
    assert idx["bgsid"].pids == []


def test_live_index_source_buckets():
    procs = [
        _sp("a", 1, "1", proc_alive=True, kind="bg", entrypoint="cli"),
        _sp(
            "b", 2, "1", proc_alive=True, kind="interactive", entrypoint="claude-vscode"
        ),
        _sp("c", 3, "1", proc_alive=True, kind="interactive", entrypoint="sdk-ts"),
        _sp("d", 4, "1", proc_alive=True, kind="interactive", entrypoint="cli"),
    ]
    idx = liveness.live_index(procs, {})
    assert idx["a"].source == "bg"
    assert idx["b"].source == "vscode"
    assert idx["c"].source == "sdk"
    assert idx["d"].source == "cli"


# --- _scrub_dead_pids: dead agents_map pids must not count as alive ---


def test_scrub_blanks_dead_pids_keeps_entries():
    mapping = {"deadsid": 4242, "livesid": 111, "settled": None}
    out = liveness._scrub_dead_pids(mapping, exists=lambda pid: pid == 111)
    assert out == {"deadsid": None, "livesid": 111, "settled": None}


def test_scrubbed_dead_pid_does_not_override_proc_verdict():
    # Registry proved the pid dead; a stale agents entry with a dead pid must
    # not flip the sid back to alive once scrubbed.
    dead_proc = _sp("sid", 4242, "123")
    scrubbed = liveness._scrub_dead_pids({"sid": 9999}, exists=lambda pid: False)
    idx = liveness.live_index([dead_proc], scrubbed)
    assert idx["sid"].alive is False


def test_scrubbed_agent_only_sid_not_alive():
    scrubbed = liveness._scrub_dead_pids({"ghost": 9999}, exists=lambda pid: False)
    idx = liveness.live_index([], scrubbed)
    assert idx["ghost"].alive is False
    assert idx["ghost"].pids == []


def test_scrub_keeps_live_pid_alive_path_intact():
    scrubbed = liveness._scrub_dead_pids({"sid": 5555}, exists=lambda pid: True)
    idx = liveness.live_index([_sp("sid", 4242, "123")], scrubbed)
    assert idx["sid"].alive is True
    assert idx["sid"].pid == 5555


def test_alive_map_skips_scrub_without_proc(monkeypatch):
    # R10 degraded mode: agents_map is the only liveness source — no scrubbing.
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: False)
    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps([{"sessionId": "sid", "pid": 424242}]),
        stderr="",
    )
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    assert liveness.alive_map(max_age=0) == {"sid": 424242}
    liveness.invalidate_cache()


def test_alive_map_scrubs_with_proc(monkeypatch):
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: True)
    monkeypatch.setattr(liveness.proc, "pid_exists", lambda pid: pid == 111)
    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            [
                {"sessionId": "live", "pid": 111},
                {"sessionId": "stale", "pid": 424242},
            ]
        ),
        stderr="",
    )
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    assert liveness.alive_map(max_age=0) == {"live": 111, "stale": None}
    liveness.invalidate_cache()


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (FileNotFoundError("claude executable missing"), "executable missing"),
        (subprocess.TimeoutExpired(["claude"], 10), "timed out"),
        (
            subprocess.CompletedProcess(
                [],
                7,
                stdout='[{"sessionId":"unsafe","pid":77}]',
                stderr="daemon unavailable",
            ),
            "exit status 7",
        ),
        (
            subprocess.CompletedProcess([], 0, stdout="{bad json", stderr=""),
            "invalid JSON",
        ),
    ],
)
def test_scan_agents_reports_unavailable_source(monkeypatch, outcome, detail):
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: False)

    def run(*args, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(liveness.subprocess, "run", run)

    result = liveness.scan_agents(max_age=0.0)

    assert result.records == {}
    assert result.availability is liveness.AgentsAvailability.UNAVAILABLE
    assert result.complete is False
    assert result.issues[0].source == "claude agents --json"
    assert detail in result.issues[0].detail


def test_scan_agents_keeps_valid_records_but_marks_bad_entries_partial(monkeypatch):
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: False)
    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            [
                {"sessionId": "safe", "pid": 11},
                {"sessionId": "bad", "pid": "not-an-int"},
            ]
        ),
        stderr="",
    )
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)

    result = liveness.scan_agents(max_age=0.0)

    assert result.records == {"safe": 11}
    assert result.availability is liveness.AgentsAvailability.PARTIAL
    assert result.complete is False
    assert "entry 1" in result.issues[0].detail


def test_scan_agents_does_not_swallow_programming_typeerror(monkeypatch):
    liveness.invalidate_cache()
    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(
        liveness.json,
        "loads",
        lambda _value: (_ for _ in ()).throw(TypeError("parser bug")),
    )

    with pytest.raises(TypeError, match="parser bug"):
        liveness.scan_agents(max_age=0.0)


# --- is_rc_exposed: AC3 six-case matrix (bridge x pid_alive) ---


def test_is_rc_exposed_matrix():
    f = liveness.is_rc_exposed
    # bridge key absent -> None
    assert f(None, True) is False
    assert f(None, False) is False
    # bridge opened-then-closed -> null/None (transient), same as absent
    assert f(None, True) is False  # null is represented as None at parse time
    # bridge exposing -> a session_* string
    assert f("session_x", True) is True
    assert f("session_x", False) is False
    # empty string is not a real bridge id
    assert f("", True) is False


# --- live_session_procs: the ONE registry->proc_alive assembly point ---


def test_live_session_procs_injects_proc_liveness(tmp_path, monkeypatch):
    import json

    from cc_session_control.config import cfg
    from cc_session_control.data import proc, registry

    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    for pid, start in ((100, "10"), (200, "20")):
        (sessions / f"{pid}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "sessionId": f"sid{pid}",
                    "procStart": start,
                }
            )
        )
    monkeypatch.setattr(proc, "pid_alive", lambda pid, ps: pid == 100)

    procs = {sp.pid: sp for sp in liveness.live_session_procs(max_age=0.0)}
    assert procs[100].proc_alive is True  # injected, not the parse default
    assert procs[200].proc_alive is False


def test_live_session_procs_propagates_programming_errors(monkeypatch):
    from cc_session_control.data import registry

    def boom(max_age=5.0):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(registry, "read_session_procs", boom)
    with pytest.raises(RuntimeError, match="registry exploded"):
        liveness.live_session_procs()


def test_liveness_inputs_is_an_immutable_typed_generation_snapshot(monkeypatch):
    procs = [_sp("sid", 101, "1", proc_alive=True)]
    jobs = [AgentJob(short="sid", sid="sid", resume_sid="sid")]
    monkeypatch.setattr(
        liveness.registry,
        "scan_session_procs",
        lambda max_age=5.0: registry.RegistryScan(records=tuple(procs)),
    )
    monkeypatch.setattr(
        liveness.registry,
        "scan_agent_jobs",
        lambda max_age=5.0: registry.RegistryScan(records=tuple(jobs)),
    )
    monkeypatch.setattr(
        liveness,
        "scan_agents",
        lambda max_age=5.0: liveness.AgentsScan(records={"sid": 101}),
    )
    monkeypatch.setattr(liveness.proc, "ancestor_pids", lambda: {101})
    monkeypatch.setattr(liveness.proc, "pid_alive", lambda pid, start: True)

    inputs = liveness.liveness_inputs()

    assert isinstance(inputs, liveness.LivenessSnapshot)
    assert inputs.complete is True
    assert inputs.issues == ()
    assert inputs.session_procs == tuple(procs)
    assert inputs.agent_jobs[0].host_pid == 101
    assert inputs.cur == frozenset({101})
    assert inputs.agents_map == {"sid": 101}
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.cur = frozenset()
    with pytest.raises(TypeError):
        inputs.agents_map["new"] = 202


def test_liveness_inputs_forces_fresh_sources_for_each_generation(monkeypatch):
    ages = {"sessions": [], "jobs": [], "agents": []}
    next_pid = iter((101, 202))

    def session_procs(max_age=5.0):
        ages["sessions"].append(max_age)
        pid = next(next_pid)
        return registry.RegistryScan(
            records=(_sp(f"sid-{pid}", pid, "1", proc_alive=True),)
        )

    monkeypatch.setattr(liveness.registry, "scan_session_procs", session_procs)
    monkeypatch.setattr(
        liveness.registry,
        "scan_agent_jobs",
        lambda max_age=5.0: ages["jobs"].append(max_age) or registry.RegistryScan(),
    )
    monkeypatch.setattr(
        liveness,
        "scan_agents",
        lambda max_age=5.0: ages["agents"].append(max_age) or liveness.AgentsScan(),
    )
    monkeypatch.setattr(liveness.proc, "pid_alive", lambda pid, start: True)
    monkeypatch.setattr(liveness.proc, "ancestor_pids", lambda: set())

    first = liveness.liveness_inputs()
    second = liveness.liveness_inputs()

    assert ages == {
        "sessions": [0.0, 0.0],
        "jobs": [0.0, 0.0],
        "agents": [0.0, 0.0],
    }
    assert [sp.pid for sp in first.session_procs] == [101]
    assert [sp.pid for sp in second.session_procs] == [202]


def test_liveness_inputs_collects_multiple_low_level_source_issues(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    liveness.invalidate_cache()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text('{"pid":1,"sessionId":"safe"}')
    (sessions / "broken.json").write_text("{bad json")
    state = tmp_path / "jobs" / "agent" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"state":"running"}')
    completed = subprocess.CompletedProcess(
        [],
        3,
        stdout='[{"sessionId":"unsafe","pid":3}]',
        stderr="agent daemon failed",
    )
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(liveness.proc, "pid_alive", lambda pid, start: False)
    monkeypatch.setattr(liveness.proc, "ancestor_pids", lambda: set())

    inputs = liveness.liveness_inputs()

    assert [item.sid for item in inputs.session_procs] == ["safe"]
    assert inputs.complete is False
    assert {issue.source for issue in inputs.issues} == {
        "session registry",
        "job registry",
        "claude agents --json",
    }
    assert all(issue.detail for issue in inputs.issues)
    with pytest.raises(dataclasses.FrozenInstanceError):
        inputs.issues = ()


def test_liveness_inputs_normal_empty_sources_are_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    liveness.invalidate_cache()
    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(liveness.proc, "ancestor_pids", lambda: set())

    inputs = liveness.liveness_inputs()

    assert inputs.complete is True
    assert inputs.issues == ()
    assert inputs.session_procs == ()
    assert inputs.agent_jobs == ()
    assert inputs.agents_map == {}
