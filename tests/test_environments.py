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
from cc_session_control.models import EnvRecord


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


def test_environments_does_not_import_rc():
    # Hard invariant (D4): the passive store must not pull in rc (no cycle).
    import inspect

    src = inspect.getsource(env)
    assert "import rc" not in src
    assert "from .rc" not in src
    assert "from ..data.rc" not in src
