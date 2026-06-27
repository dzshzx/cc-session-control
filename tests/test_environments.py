"""Tests for data/environments.py — the bridge-environment ledger (AC6).

The ledger lives at `cfg.environments_ledger` (under `cfg.config_dir`); all
tests monkeypatch `cfg.config_dir` to a tmp dir. `now` is injected so
first_seen/last_seen are deterministic. The observe() builder test monkeypatches
`cfg.claude_home` and feeds registry fixtures.
"""

import json
import os

from cc_session_control.config import cfg
from cc_session_control.data import environments as env
from cc_session_control.data import registry
from cc_session_control.models import AgentJob, EnvRecord, RCServer, SessionProc


def _use_tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "config_dir", tmp_path)


def _ledger_lines(tmp_path):
    text = (tmp_path / "environments.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --- environments_ledger path follows config_dir ---------------------------

def test_ledger_path_follows_config_dir(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    assert cfg.environments_ledger == tmp_path / "environments.jsonl"


# --- accumulation + injected clock -----------------------------------------

def test_upsert_accumulates_with_injected_now(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=100.0)
    env.upsert([EnvRecord("session", "BBB", "sid-b")], now=200.0)

    rows = {(r["prefix"], r["key"]): r for r in _ledger_lines(tmp_path)}
    assert set(rows) == {("cse", "AAA"), ("session", "BBB")}
    assert rows[("cse", "AAA")]["first_seen"] == 100.0
    assert rows[("cse", "AAA")]["last_seen"] == 100.0
    assert rows[("session", "BBB")]["first_seen"] == 200.0


def test_upsert_reobservation_advances_last_seen_keeps_first_seen(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=100.0)
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=500.0)

    (row,) = _ledger_lines(tmp_path)
    assert row["first_seen"] == 100.0   # preserved
    assert row["last_seen"] == 500.0    # advanced


def test_upsert_reobservation_updates_bound_sid(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    # resume of an agent keeps the cse_ env but binds a new sid.
    env.upsert([EnvRecord("cse", "AAA", "sid-old")], now=100.0)
    env.upsert([EnvRecord("cse", "AAA", "sid-new")], now=200.0)

    (row,) = _ledger_lines(tmp_path)
    assert row["bound_sid"] == "sid-new"
    assert row["first_seen"] == 100.0


# --- write-on-change -------------------------------------------------------

def test_identical_upsert_does_not_rewrite(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=100.0)
    path = tmp_path / "environments.jsonl"
    first_mtime = path.stat().st_mtime_ns
    first_text = path.read_text()

    # Same records, SAME now -> serialized content is identical -> no rewrite.
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=100.0)
    assert path.stat().st_mtime_ns == first_mtime
    assert path.read_text() == first_text


def test_no_temp_file_left_behind(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=100.0)
    assert not os.path.exists(str(tmp_path / "environments.jsonl.tmp"))


def test_atomic_write_produces_valid_jsonl(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert(
        [EnvRecord("cse", "AAA", "sid-a"), EnvRecord("session", "BBB", "sid-b")],
        now=100.0,
    )
    text = (tmp_path / "environments.jsonl").read_text()
    for line in text.splitlines():
        assert json.loads(line)  # every line is valid JSON
    assert text.endswith("\n")


# --- namespace-scoped dedup ------------------------------------------------

def test_within_cse_suffix_dedup_resume_pair(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    # Two jobs share one cse_ suffix (a resume pair) -> ONE env.
    env.upsert(
        [
            EnvRecord("cse", "SHARED", "sid-1"),
            EnvRecord("cse", "SHARED", "sid-2"),
        ],
        now=100.0,
    )
    rows = _ledger_lines(tmp_path)
    assert len(rows) == 1
    assert (rows[0]["prefix"], rows[0]["key"]) == ("cse", "SHARED")


def test_session_and_cse_same_suffix_not_merged(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    # Even if suffixes coincide (they don't in practice), namespaces stay split.
    env.upsert(
        [
            EnvRecord("session", "SAME", "sid-s"),
            EnvRecord("cse", "SAME", "sid-c"),
        ],
        now=100.0,
    )
    rows = {(r["prefix"], r["key"]) for r in _ledger_lines(tmp_path)}
    assert rows == {("session", "SAME"), ("cse", "SAME")}


# --- current vs orphan split -----------------------------------------------

def test_current_vs_orphan_split(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert(
        [EnvRecord("cse", "AAA", "sid-a"), EnvRecord("session", "BBB", "sid-b")],
        now=100.0,
    )
    # Only AAA is observed now -> AAA current, BBB orphan.
    observed = [EnvRecord("cse", "AAA", "sid-a")]

    current = env.current_envs(observed)
    orphans = env.orphan_envs(observed)
    assert [(e.prefix, e.key, e.status) for e in current] == [("cse", "AAA", "current")]
    assert [(e.prefix, e.key, e.status) for e in orphans] == [("session", "BBB", "orphan")]


def test_current_includes_observed_not_yet_in_ledger(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    # Query before any upsert: observed env is still reported current.
    observed = [EnvRecord("cse", "NEW", "sid-x")]
    current = env.current_envs(observed)
    assert [(e.prefix, e.key, e.status) for e in current] == [("cse", "NEW", "current")]
    assert env.orphan_envs(observed) == []


def test_orphan_split_when_nothing_observed(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=100.0)
    assert env.current_envs([]) == []
    orphans = env.orphan_envs([])
    assert [(e.prefix, e.key) for e in orphans] == [("cse", "AAA")]


# --- manual delete list ----------------------------------------------------

def test_manual_delete_list_includes_env_namespace(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    # env_* is the project RC server namespace (pushed in by rc in Phase 5);
    # here we upsert one directly to prove it is a manual-delete candidate.
    env.upsert(
        [
            EnvRecord("cse", "AAA", "sid-a"),
            EnvRecord("env", "ENVKEY", None),
        ],
        now=100.0,
    )
    # Nothing observed -> both are orphans / candidates.
    rows = env.manual_delete_list()
    ids = {r["env_id"] for r in rows}
    assert ids == {"cse_AAA", "env_ENVKEY"}
    env_row = next(r for r in rows if r["env_id"] == "env_ENVKEY")
    assert env_row["prefix"] == "env"
    assert env_row["last_seen"] == 100.0


def test_manual_delete_list_excludes_currently_observed(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    env.upsert(
        [EnvRecord("cse", "AAA", "sid-a"), EnvRecord("session", "BBB", "sid-b")],
        now=100.0,
    )
    rows = env.manual_delete_list([EnvRecord("cse", "AAA", "sid-a")])
    assert {r["env_id"] for r in rows} == {"session_BBB"}


# --- corrupt / missing ledger is safe --------------------------------------

def test_missing_ledger_is_safe(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    assert env.current_envs([]) == []
    assert env.orphan_envs([]) == []
    assert env.manual_delete_list() == []


def test_corrupt_ledger_lines_skipped(tmp_path, monkeypatch):
    _use_tmp_ledger(tmp_path, monkeypatch)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "environments.jsonl"
    good = json.dumps({"prefix": "cse", "key": "AAA", "bound_sid": "sid-a",
                       "first_seen": 1.0, "last_seen": 2.0})
    path.write_text("{not json\n" + good + "\n{\"prefix\": \"\", \"key\": \"x\"}\n")

    orphans = env.orphan_envs([])
    assert [(e.prefix, e.key) for e in orphans] == [("cse", "AAA")]
    # A later upsert merges cleanly on top of the salvaged entry.
    env.upsert([EnvRecord("cse", "AAA", "sid-a")], now=9.0)
    (row,) = _ledger_lines(tmp_path)
    assert row["first_seen"] == 1.0   # preserved from the salvaged line
    assert row["last_seen"] == 9.0


# --- observe() builder reads registry, not rc ------------------------------

def test_observe_builds_from_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "1.json").write_text(json.dumps({
        "pid": 1, "sessionId": "sid-s",
        "bridgeSessionId": "session_016spR3Nkq2tJL2edM1exfuo",
    }))
    # a session with no bridge -> not observed
    (sessions / "2.json").write_text(json.dumps({"pid": 2, "sessionId": "sid-nobridge"}))
    jobs = tmp_path / "jobs" / "0877f45e"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / "state.json").write_text(json.dumps({
        "sessionId": "0877f45e-x", "bridgeSessionId": "cse_01DgeqMqXMrSFpW59uSZwK99",
    }))

    records = {(r.prefix, r.key): r for r in env.observe(max_age=0.0)}
    assert set(records) == {
        ("session", "016spR3Nkq2tJL2edM1exfuo"),
        ("cse", "01DgeqMqXMrSFpW59uSZwK99"),
    }
    assert records[("session", "016spR3Nkq2tJL2edM1exfuo")].bound_sid == "sid-s"


# --- observe(): bridge-truthy FILE-REFERENCED set (R6 ledger membership) -----

def _sp(pid, sid, bridge=None, proc_alive=False, proc_start="1"):
    return SessionProc(pid=pid, sid=sid, bridge=bridge,
                       proc_alive=proc_alive, proc_start=proc_start)


def test_observe_includes_zombie_bridge_unlike_observe_live():
    # observe() is MEMBERSHIP (file-referenced), NOT alive-gated: a zombie
    # session's stale bridge is still part of the cloud and stays in the ledger.
    procs = [
        _sp(1, "sid-alive", bridge="session_ALIVE", proc_alive=True),
        _sp(2, "sid-dead", bridge="session_ZOMBIE", proc_alive=False),
    ]
    file_ref = {(r.prefix, r.key) for r in env.observe(procs, [])}
    assert file_ref == {("session", "ALIVE"), ("session", "ZOMBIE")}
    live = {(r.prefix, r.key) for r in env.observe_live(procs, [])}
    assert live == {("session", "ALIVE")}


def test_observe_includes_env_from_rc_servers_regardless_of_status():
    # env_* has no state file; membership = referenced by a server passed in,
    # running OR dead (orphan-ing is handled by it dropping out next cycle).
    servers = [
        RCServer(name="ws/a", env_id="env_RUN", status="running"),
        RCServer(name="ws/b", env_id="env_DEADSRV", status="dead"),
    ]
    file_ref = {(r.prefix, r.key) for r in env.observe([], [], servers)}
    assert file_ref == {("env", "RUN"), ("env", "DEADSRV")}


def test_orphan_appears_after_env_toggles_away(tmp_path, monkeypatch):
    # The whole point of R6: cycle 1 a file references env X (persist it); cycle 2
    # the file no longer references it -> X becomes an orphan / manual-delete.
    _use_tmp_ledger(tmp_path, monkeypatch)
    file_ref1 = env.observe([_sp(1, "sid-x", bridge="session_X", proc_alive=True)], [])
    env.upsert(file_ref1, now=100.0)

    file_ref2 = env.observe([_sp(1, "sid-x", bridge=None, proc_alive=True)], [])
    env.upsert(file_ref2, now=200.0)

    assert file_ref2 == []
    orphans = env.orphan_envs(file_ref2)
    assert [e.env_id for e in orphans] == ["session_X"]
    assert {r["env_id"] for r in env.manual_delete_list(file_ref2)} == {"session_X"}


def test_file_referenced_zombie_is_neither_current_nor_orphan(tmp_path, monkeypatch):
    # A zombie's bridge is still file-referenced: alive-gated CURRENT excludes it,
    # but it is NOT an orphan either (a file still references it) — it sits in the
    # middle tier active(alive) ⊆ file-referenced ⊆ ledger.
    _use_tmp_ledger(tmp_path, monkeypatch)
    procs = [_sp(2, "sid-dead", bridge="session_ZOMBIE", proc_alive=False)]
    file_ref = env.observe(procs, [])
    env.upsert(file_ref, now=100.0)
    observed = env.observe_live(procs, [])

    current = env.current_envs(observed)
    orphans = env.orphan_envs(file_ref)
    assert all(e.env_id != "session_ZOMBIE" for e in current)   # not current (dead)
    assert all(e.env_id != "session_ZOMBIE" for e in orphans)   # not orphan (referenced)


# --- observe_live(): alive-gated CURRENT set (R3/R6, zombie not current) ----


def test_observe_live_excludes_dead_session_bridge():
    # A zombie session proc with a stale bridge must NOT be reported (it would be
    # an orphan, not current) — this is the bug the alive-gate fixes.
    procs = [
        _sp(1, "sid-alive", bridge="session_ALIVE", proc_alive=True),
        _sp(2, "sid-dead", bridge="session_ZOMBIE", proc_alive=False),
    ]
    recs = {(r.prefix, r.key) for r in env.observe_live(procs, [])}
    assert recs == {("session", "ALIVE")}


def test_observe_live_zombie_not_current_via_classifier():
    # End-to-end: feed observe_live's output into current_envs — the zombie's
    # stale bridge must not land in the current set.
    procs = [_sp(2, "sid-dead", bridge="session_ZOMBIE", proc_alive=False)]
    observed = env.observe_live(procs, [])
    current = env.current_envs(observed)
    assert all(e.env_id != "session_ZOMBIE" for e in current)


def test_observe_live_cse_gated_by_host_alive():
    job_live = AgentJob(short="a", sid="sid-bg-live", resume_sid="sid-bg-live",
                        env_suffix="LIVE", host_alive=True)
    job_dead = AgentJob(short="b", sid="sid-bg-dead", resume_sid="sid-bg-dead",
                        env_suffix="DEAD", host_alive=False)
    recs = {(r.prefix, r.key) for r in env.observe_live([], [job_live, job_dead])}
    assert recs == {("cse", "LIVE")}


def test_observe_live_cse_gated_by_alive_sid_fallback():
    # When jobs aren't host-enriched (host_alive=False), a proc-alive session
    # sharing the job sid still makes the cse_ env current.
    procs = [_sp(3, "sid-bg", proc_alive=True)]
    job = AgentJob(short="c", sid="sid-bg", resume_sid="sid-bg",
                   env_suffix="VIASID", host_alive=False)
    recs = {(r.prefix, r.key) for r in env.observe_live(procs, [job])}
    assert recs == {("cse", "VIASID")}


def test_observe_live_env_gated_by_running_server():
    running = RCServer(name="ws/a", env_id="env_RUN", status="running")
    dead = RCServer(name="ws/b", env_id="env_DEAD", status="dead")
    recs = {(r.prefix, r.key) for r in env.observe_live([], [], [running, dead])}
    assert recs == {("env", "RUN")}


def test_environments_does_not_import_rc():
    # Hard invariant (D4): the passive store must not pull in rc (no cycle).
    import inspect

    src = inspect.getsource(env)
    assert "import rc" not in src
    assert "from .rc" not in src
    assert "from ..data.rc" not in src
