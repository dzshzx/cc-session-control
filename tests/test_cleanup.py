"""Tests for data/cleanup.py — two strategies, per-dir key semantics (AC7) and
the no-`/proc` degraded-refuse safety net (AC10).

All filesystem tests monkeypatch `cfg.claude_home` to a tmp dir; the degraded
path is forced by monkeypatching `proc.has_proc -> False` (so
`current_determinable()` is False) — never the real `~/.claude`.
"""

import json
import os

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import cleanup, liveness, registry
from cc_session_control.data import proc as proc_mod
from cc_session_control.models import AgentJob, Session, SessionProc


@pytest.fixture(autouse=True)
def _hermetic_liveness(monkeypatch):
    """Keep the orphan protected-sid self-fetch (H1) off the network/subprocess.

    `list_orphan_dirs` now consults `liveness.alive_map` (`claude agents --json`)
    and the registry when not given injected data; stub the subprocess and reset
    the registry TTL cache so cleanup tests stay hermetic and deterministic.
    """
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


def _make_session(**overrides) -> Session:
    defaults = dict(
        sid="abc123", cwd="/tmp/proj", label="t", mtime=0.0,
        prompts=0, pid=None, alive=False, current=False,
        file="/tmp/abc123.jsonl",
    )
    defaults.update(overrides)
    return Session(**defaults)


def _mkdir(base, *parts):
    p = os.path.join(str(base), *parts)
    os.makedirs(p, exist_ok=True)
    return p


# --- Strategy A: sid-keyed orphan dirs (per-dir semantics) -----------------

def test_list_orphan_dirs_sid_keyed_per_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    known = "keep-sid"
    # known sid is referenced by a session -> not an orphan in any sid dir.
    _mkdir(tmp_path, "session-env", known)
    _mkdir(tmp_path, "session-env", "orphan-a")
    _mkdir(tmp_path, "file-history", "orphan-b")
    _mkdir(tmp_path, "tasks", "orphan-c")
    _mkdir(tmp_path, "uploads", "orphan-d")

    orphans = cleanup.list_orphan_dirs([_make_session(sid=known)])
    assert orphans == [
        "file-history/orphan-b",
        "session-env/orphan-a",
        "tasks/orphan-c",
        "uploads/orphan-d",
    ]


def test_debug_dir_not_treated_as_sid_orphan(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    # debug uuids are debug-run ids, NOT sessionIds -> never sid-orphans.
    _mkdir(tmp_path, "debug", "11111111-2222-3333-4444-555555555555")
    _mkdir(tmp_path, "session-env", "orphan-a")

    orphans = cleanup.list_orphan_dirs([_make_session(sid="some-other-sid")])
    assert orphans == ["session-env/orphan-a"]
    assert not any(o.startswith("debug/") for o in orphans)


def test_remove_orphan_dirs_keeps_known(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    known = "keep-sid"
    _mkdir(tmp_path, "session-env", known)
    _mkdir(tmp_path, "session-env", "orphan-a")
    _mkdir(tmp_path, "file-history", "orphan-b")

    count = cleanup.remove_orphan_dirs([_make_session(sid=known)])
    assert count == 2
    assert os.path.isdir(os.path.join(tmp_path, "session-env", known))
    assert not os.path.exists(os.path.join(tmp_path, "session-env", "orphan-a"))
    assert not os.path.exists(os.path.join(tmp_path, "file-history", "orphan-b"))


# --- H1: orphan sweep protects registry-known / live / current sids ---------

def _job(sid, **kw):
    base = dict(short=sid[:8], sid=sid, resume_sid=sid)
    base.update(kw)
    return AgentJob(**base)


def test_orphan_sweep_keeps_live_bg_agent_artifacts(tmp_path, monkeypatch):
    # (a) A LIVE background agent has no transcript Session, but its
    # file-history/<sid> must NOT be swept (host_alive protects it).
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    live_sid = "live-bg-sid"
    _mkdir(tmp_path, "file-history", live_sid)
    job = _job(live_sid, host_alive=True)

    inject = dict(session_procs=[], agent_jobs=[job], agents_map={}, cur=set())
    assert cleanup.list_orphan_dirs([], **inject) == []
    assert cleanup.remove_orphan_dirs([], **inject) == 0
    assert os.path.isdir(os.path.join(tmp_path, "file-history", live_sid))


def test_orphan_sweep_keeps_registry_known_sid_without_transcript(tmp_path, monkeypatch):
    # (b) A sid known ONLY via sessions/<pid>.json (transcript dropped) must not
    # be swept — even when its proc is dead (registry membership protects it).
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    reg_sid = "registry-only-sid"
    _mkdir(tmp_path, "uploads", reg_sid)
    sp = SessionProc(pid=4242, sid=reg_sid, proc_start="1", proc_alive=False)

    inject = dict(session_procs=[sp], agent_jobs=[], agents_map={}, cur=set())
    assert cleanup.list_orphan_dirs([], **inject) == []
    assert cleanup.remove_orphan_dirs([], **inject) == 0
    assert os.path.isdir(os.path.join(tmp_path, "uploads", reg_sid))


def test_orphan_sweep_removes_genuinely_unknown_dead_sid(tmp_path, monkeypatch):
    # (c) A sid in NONE of transcript/registry/live/current IS still swept.
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _mkdir(tmp_path, "session-env", "ghost-sid")

    inject = dict(session_procs=[], agent_jobs=[], agents_map={}, cur=set())
    assert cleanup.list_orphan_dirs([], **inject) == ["session-env/ghost-sid"]
    assert cleanup.remove_orphan_dirs([], **inject) == 1
    assert not os.path.exists(os.path.join(tmp_path, "session-env", "ghost-sid"))


def test_orphan_sweep_keeps_alive_map_sid(tmp_path, monkeypatch):
    # A sid live only via `claude agents --json` (alive_map) is protected too.
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    live_sid = "agents-json-sid"
    _mkdir(tmp_path, "tasks", live_sid)

    inject = dict(session_procs=[], agent_jobs=[], agents_map={live_sid: 999}, cur=set())
    assert cleanup.list_orphan_dirs([], **inject) == []
    assert os.path.isdir(os.path.join(tmp_path, "tasks", live_sid))


# --- Strategy A: pid-keyed zombie session files (multi-pid) -----------------

def _sp(pid, sid, proc_alive):
    return SessionProc(pid=pid, sid=sid, proc_start=str(pid), proc_alive=proc_alive)


def test_select_zombie_pids_multi_pid_and_current():
    # sid "A" resumed: 700772 dead, 710575 alive -> drop the dead pid file only.
    # pid 1001 is csctl's current session -> protected even though dead.
    procs = [
        _sp(700772, "A", proc_alive=False),  # zombie -> removable
        _sp(710575, "A", proc_alive=True),   # alive, SAME sid -> keep
        _sp(1001, "B", proc_alive=False),    # dead but current -> keep
    ]
    assert cleanup.select_zombie_pids(procs, cur={1001}) == [700772]


def test_remove_zombie_session_files_multi_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    sessions_dir = _mkdir(tmp_path, "sessions")
    for pid in (700772, 710575, 1001):
        with open(os.path.join(sessions_dir, f"{pid}.json"), "w") as fh:
            json.dump({"pid": pid}, fh)
    procs = [
        _sp(700772, "A", proc_alive=False),
        _sp(710575, "A", proc_alive=True),
        _sp(1001, "B", proc_alive=False),
    ]

    count = cleanup.remove_zombie_session_files(procs, cur={1001})
    assert count == 1
    assert not os.path.exists(os.path.join(sessions_dir, "700772.json"))
    assert os.path.exists(os.path.join(sessions_dir, "710575.json"))  # alive kept
    assert os.path.exists(os.path.join(sessions_dir, "1001.json"))    # current kept


# --- Strategy B: age sweep (controllable mtime) ----------------------------

def test_list_aged_entries_uses_cleanup_age_days(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = 1_000_000_000.0
    old = now - 20 * 86400   # older than 14d -> swept
    recent = now - 1 * 86400  # within 14d -> kept

    snap = _mkdir(tmp_path, "shell-snapshots")
    old_file = os.path.join(snap, "old.sh")
    new_file = os.path.join(snap, "new.sh")
    open(old_file, "w").close()
    open(new_file, "w").close()
    os.utime(old_file, (old, old))
    os.utime(new_file, (recent, recent))

    plans = _mkdir(tmp_path, "plans")
    old_plan = os.path.join(plans, "p1")
    open(old_plan, "w").close()
    os.utime(old_plan, (old, old))

    aged = cleanup.list_aged_entries(now=now)
    assert aged == ["plans/p1", "shell-snapshots/old.sh"]


def test_remove_aged_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = 1_000_000_000.0
    old = now - 30 * 86400
    recent = now - 2 * 86400

    snap = _mkdir(tmp_path, "shell-snapshots")
    for name, mtime in (("old.sh", old), ("new.sh", recent)):
        f = os.path.join(snap, name)
        open(f, "w").close()
        os.utime(f, (mtime, mtime))

    count = cleanup.remove_aged_entries(now=now)
    assert count == 1
    assert not os.path.exists(os.path.join(snap, "old.sh"))
    assert os.path.exists(os.path.join(snap, "new.sh"))


# --- Classified counts (injected deps) -------------------------------------

def test_cleanup_classified_breaks_down_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    _mkdir(tmp_path, "session-env", "orphan-a")
    sessions = [
        _make_session(sid="empty1", prompts=0),
        _make_session(sid="short1", prompts=2),
        _make_session(sid="full1", prompts=9),
    ]
    procs = [_sp(700772, "A", proc_alive=False)]  # one zombie

    counts = cleanup.cleanup_classified(sessions, procs, cur=set(), now=0.0)
    assert counts["empty"] == 1
    assert counts["short"] == 1
    assert counts["orphan_dirs"] == 1
    assert counts["zombie_procs"] == 1
    assert counts["aged_entries"] == 0


# --- AC10: degraded (no /proc) refuses destructive ops ---------------------

def _degrade(monkeypatch):
    monkeypatch.setattr(proc_mod, "has_proc", lambda: False)


def test_prune_refuses_without_proc(tmp_path, monkeypatch):
    import time
    _degrade(monkeypatch)
    old = time.time() - 700
    sessions = [_make_session(sid="dead", prompts=0, mtime=old, alive=False)]
    assert cleanup.prune_sessions(sessions, max_prompts=0) == []


def test_list_and_remove_orphans_refuse_without_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _mkdir(tmp_path, "session-env", "orphan-a")
    _degrade(monkeypatch)
    sessions = [_make_session(sid="keep")]
    assert cleanup.list_orphan_dirs(sessions) == []
    assert cleanup.remove_orphan_dirs(sessions) == 0
    # nothing deleted while degraded
    assert os.path.isdir(os.path.join(tmp_path, "session-env", "orphan-a"))


def test_remove_zombie_refuses_without_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    sessions_dir = _mkdir(tmp_path, "sessions")
    f = os.path.join(sessions_dir, "700772.json")
    open(f, "w").close()
    _degrade(monkeypatch)
    procs = [_sp(700772, "A", proc_alive=False)]
    assert cleanup.remove_zombie_session_files(procs, cur=set()) == 0
    assert os.path.exists(f)  # zombie survives — can't tell current apart


def test_remove_session_refuses_without_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    projects = _mkdir(tmp_path, "projects", "proj1")
    transcript = os.path.join(projects, "sid1.jsonl")
    open(transcript, "w").close()
    _degrade(monkeypatch)
    assert cleanup.remove_session(_make_session(sid="sid1", file=transcript)) is False
    assert os.path.exists(transcript)  # not deleted while degraded


# --- M3: remove_session must not delete a LIVE agent's jobs dir -------------

def test_remove_session_keeps_live_agents_jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    sid = "abcdef0123456789"
    projects = _mkdir(tmp_path, "projects", "proj1")
    transcript = os.path.join(projects, f"{sid}.jsonl")
    open(transcript, "w").close()
    # A live host pid for this sid (registry sessions file + proc-alive).
    sessions_dir = _mkdir(tmp_path, "sessions")
    with open(os.path.join(sessions_dir, "5555.json"), "w") as fh:
        json.dump({"pid": 5555, "sessionId": sid, "procStart": "999"}, fh)
    monkeypatch.setattr(cleanup.proc, "pid_alive", lambda pid, ps: pid == 5555)
    registry.invalidate_cache()
    # Its jobs/<short> dir + a sid-keyed artifact dir.
    jobs_dir = _mkdir(tmp_path, "jobs", sid[:8])
    open(os.path.join(jobs_dir, "state.json"), "w").close()
    se_dir = _mkdir(tmp_path, "session-env", sid)

    assert cleanup.remove_session(_make_session(sid=sid, file=transcript)) is True
    # The live agent's jobs dir is preserved (M3) ...
    assert os.path.isdir(jobs_dir)
    # ... while the transcript and ordinary sid artifacts are still removed.
    assert not os.path.exists(transcript)
    assert not os.path.exists(se_dir)


def test_remove_session_removes_jobs_dir_when_no_live_host(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    sid = "fedcba9876543210"
    projects = _mkdir(tmp_path, "projects", "proj1")
    transcript = os.path.join(projects, f"{sid}.jsonl")
    open(transcript, "w").close()
    registry.invalidate_cache()  # no sessions/<pid>.json -> no live host
    jobs_dir = _mkdir(tmp_path, "jobs", sid[:8])
    open(os.path.join(jobs_dir, "state.json"), "w").close()

    assert cleanup.remove_session(_make_session(sid=sid, file=transcript)) is True
    assert not os.path.exists(jobs_dir)  # settled -> jobs dir removed


def test_terminate_refuses_without_proc(monkeypatch):
    import cc_session_control.actions.session_ops as so

    killed = {"n": 0}
    monkeypatch.setattr(so.os, "kill", lambda *_: killed.__setitem__("n", killed["n"] + 1))
    monkeypatch.setattr(so.proc, "has_proc", lambda: False)

    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    assert so.terminate_session(s) is False
    assert killed["n"] == 0  # no SIGTERM fired while current is undeterminable
