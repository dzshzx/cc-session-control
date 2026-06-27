"""Tests for data/liveness.py — live_index purity and the agents.py cache shim."""

import time

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


# --- agents.py shim shares ONE cache with liveness ---

def test_shim_and_liveness_share_one_cache():
    from cc_session_control.data import agents
    # Same callables -> same module-global cache.
    assert agents.alive_map is liveness.alive_map
    assert agents.invalidate_cache is liveness.invalidate_cache

    liveness._cache = {"sid1": 111}
    liveness._cache_time = time.monotonic()
    # Reading through the shim returns the seeded cache (no subprocess).
    assert agents.alive_map(max_age=999) == {"sid1": 111}
    # Invalidating through the shim clears liveness's single cache.
    agents.invalidate_cache()
    assert liveness._cache is None
