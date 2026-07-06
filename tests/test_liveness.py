"""Tests for data/liveness.py — live_index purity and the alive_map cache."""

from cc_session_control.data import liveness
from cc_session_control.models import SessionProc


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
        _sp("sid", 700001, "150"),                   # dead -> excluded from pids
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
        _sp("b", 2, "1", proc_alive=True, kind="interactive", entrypoint="claude-vscode"),
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
    import json as _json
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: False)

    class _CP:
        stdout = _json.dumps([{"sessionId": "sid", "pid": 424242}])

    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: _CP())
    assert liveness.alive_map(max_age=0) == {"sid": 424242}
    liveness.invalidate_cache()


def test_alive_map_scrubs_with_proc(monkeypatch):
    import json as _json
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: True)
    monkeypatch.setattr(liveness.proc, "pid_exists", lambda pid: pid == 111)

    class _CP:
        stdout = _json.dumps([
            {"sessionId": "live", "pid": 111},
            {"sessionId": "stale", "pid": 424242},
        ])

    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: _CP())
    assert liveness.alive_map(max_age=0) == {"live": 111, "stale": None}
    liveness.invalidate_cache()


# --- _is_rc_exposed: AC3 six-case matrix (bridge x pid_alive) ---

def test_is_rc_exposed_matrix():
    f = liveness._is_rc_exposed
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
