"""Tests for data/registry.py — parsing sessions/*.json and jobs/*/state.json."""

import json

from cc_session_control.config import cfg
from cc_session_control.data import registry
from cc_session_control.models import SessionProc


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def test_read_session_procs(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    _write_json(sessions / "151818.json", {
        "pid": 151818, "sessionId": "sid-aaa", "cwd": "/work/a",
        "kind": "bg", "entrypoint": "cli", "status": "idle",
        "procStart": "7601319", "version": "2.1.183",
    })
    _write_json(sessions / "2347.json", {
        "pid": 2347, "sessionId": "sid-bbb", "cwd": "/work/b",
        "kind": "bg", "entrypoint": "cli", "status": "idle",
        "procStart": "9419", "version": "2.1.178",
        "bridgeSessionId": "session_016spR3Nkq2tJL2edM1exfuo",
    })
    # malformed file -> skipped, never raises
    (sessions / "broken.json").write_text("{not json")
    # missing pid/sid -> skipped
    _write_json(sessions / "nopid.json", {"sessionId": "sid-ccc"})

    rows = {r.sid: r for r in registry.read_session_procs(max_age=0.0)}
    assert set(rows) == {"sid-aaa", "sid-bbb"}
    assert rows["sid-aaa"].pid == 151818
    assert rows["sid-aaa"].proc_start == "7601319"
    assert rows["sid-aaa"].proc_alive is False
    assert rows["sid-aaa"].bridge is None
    assert rows["sid-bbb"].bridge == "session_016spR3Nkq2tJL2edM1exfuo"
    assert rows["sid-bbb"].cwd == "/work/b"


def test_read_session_procs_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    assert registry.read_session_procs(max_age=0.0) == []


def test_read_agent_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    jobs = tmp_path / "jobs"
    _write_json(jobs / "0877f45e" / "state.json", {
        "state": "stopped", "tempo": "idle", "cwd": "/work/local",
        "name": "关闭沙箱环境",
        "respawnFlags": ["--reply-on-resume", "--effort", "xhigh"],
        "sessionId": "0877f45e-04ac-4413-b9a7-54adf8af1ca5",
        "resumeSessionId": "0877f45e-04ac-4413-b9a7-54adf8af1ca5",
        "bridgeSessionId": "cse_01DgeqMqXMrSFpW59uSZwK99",
        "backend": "daemon",
    })
    # second job: missing optional fields, no bridge
    _write_json(jobs / "abcd1234" / "state.json", {
        "state": "running", "sessionId": "abcd1234-xxxx",
    })
    # non-state files in jobs dir are ignored by the glob
    (jobs / "stray.txt").write_text("ignore me")

    rows = {r.short: r for r in registry.read_agent_jobs(max_age=0.0)}
    assert set(rows) == {"0877f45e", "abcd1234"}

    j = rows["0877f45e"]
    assert j.sid == "0877f45e-04ac-4413-b9a7-54adf8af1ca5"
    assert j.resume_sid == "0877f45e-04ac-4413-b9a7-54adf8af1ca5"
    assert j.state == "stopped"
    assert j.cwd == "/work/local"
    assert j.name == "关闭沙箱环境"
    assert j.env_suffix == "01DgeqMqXMrSFpW59uSZwK99"  # suffix of cse_*
    assert j.respawn_flags == ["--reply-on-resume", "--effort", "xhigh"]
    # state.json carries NO pid -> these default until joined later (Phase 6)
    assert j.host_pid is None
    assert j.host_alive is False

    j2 = rows["abcd1234"]
    assert j2.resume_sid == "abcd1234-xxxx"  # falls back to sessionId
    assert j2.env_suffix == ""
    assert j2.respawn_flags == []


def test_read_agent_jobs_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    assert registry.read_agent_jobs(max_age=0.0) == []


# --- host_pid_for_sid: the single pure host-pid join (item 6) ---

def _sp(pid, sid, proc_alive):
    return SessionProc(pid=pid, sid=sid, proc_start=str(pid), proc_alive=proc_alive)


def test_host_pid_for_sid_prefers_proc_alive_match():
    # resume mints a new pid for the same sid; prefer the proc-alive one.
    procs = [_sp(100, "sid-a", False), _sp(200, "sid-a", True), _sp(300, "other", True)]
    assert registry.host_pid_for_sid("sid-a", procs) == (200, True)


def test_host_pid_for_sid_falls_back_to_first_dead():
    procs = [_sp(100, "sid-a", False), _sp(101, "sid-a", False)]
    assert registry.host_pid_for_sid("sid-a", procs) == (100, False)


def test_host_pid_for_sid_none_when_unknown():
    assert registry.host_pid_for_sid("sid-missing", [_sp(100, "sid-a", True)]) == (None, False)


def test_registry_cache_reuses_until_invalidated(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    _write_json(sessions / "1.json", {"pid": 1, "sessionId": "s1"})

    first = registry.read_session_procs(max_age=999)
    assert {r.sid for r in first} == {"s1"}

    # add a file; cached read (large max_age) should NOT see it yet
    _write_json(sessions / "2.json", {"pid": 2, "sessionId": "s2"})
    cached = registry.read_session_procs(max_age=999)
    assert {r.sid for r in cached} == {"s1"}

    # invalidate -> fresh read picks it up
    registry.invalidate_cache()
    fresh = registry.read_session_procs(max_age=999)
    assert {r.sid for r in fresh} == {"s1", "s2"}
