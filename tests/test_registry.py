"""Tests for data/registry.py — parsing sessions/*.json."""

import builtins
import json
import os

import pytest

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
    _write_json(
        sessions / "151818.json",
        {
            "pid": 151818,
            "sessionId": "sid-aaa",
            "cwd": "/work/a",
            "kind": "bg",
            "entrypoint": "cli",
            "status": "idle",
            "procStart": "7601319",
            "version": "2.1.183",
        },
    )
    _write_json(
        sessions / "2347.json",
        {
            "pid": 2347,
            "sessionId": "sid-bbb",
            "cwd": "/work/b",
            "kind": "bg",
            "entrypoint": "cli",
            "status": "idle",
            "procStart": "9419",
            "version": "2.1.178",
            "bridgeSessionId": "session_016spR3Nkq2tJL2edM1exfuo",
        },
    )
    # malformed file -> skipped, never raises
    (sessions / "broken.json").write_text("{not json")
    # missing pid/sid -> skipped
    _write_json(sessions / "nopid.json", {"sessionId": "sid-ccc"})

    rows = {r.sid: r for r in registry.read_session_procs(max_age=0.0)}
    assert set(rows) == {"sid-aaa", "sid-bbb"}
    assert rows["sid-aaa"].pid == 151818
    assert rows["sid-aaa"].proc_start == "7601319"
    assert rows["sid-aaa"].proc_alive is None  # raw parse: liveness NOT injected
    assert rows["sid-aaa"].bridge is None
    assert rows["sid-bbb"].bridge == "session_016spR3Nkq2tJL2edM1exfuo"
    assert rows["sid-bbb"].cwd == "/work/b"


def test_read_session_procs_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    assert registry.read_session_procs(max_age=0.0) == []


def test_scan_session_procs_keeps_partial_records_and_reports_malformed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    _write_json(sessions / "1.json", {"pid": 1, "sessionId": "sid-ok"})
    malformed = sessions / "broken.json"
    malformed.write_text("{not json")
    invalid = sessions / "invalid.json"
    _write_json(invalid, {"sessionId": "missing-pid"})

    result = registry.scan_session_procs(max_age=0.0)

    assert [row.sid for row in result.records] == ["sid-ok"]
    assert result.complete is False
    assert {issue.path for issue in result.issues} == {
        os.fspath(malformed),
        os.fspath(invalid),
    }
    assert all(issue.source == "session registry" for issue in result.issues)


def test_scan_session_procs_reports_read_oserror_but_ignores_file_race(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    denied = sessions / "denied.json"
    vanished = sessions / "vanished.json"
    _write_json(denied, {"pid": 1, "sessionId": "sid-denied"})
    _write_json(vanished, {"pid": 2, "sessionId": "sid-vanished"})
    original_open = builtins.open

    def open_with_failures(file, *args, **kwargs):
        if os.fspath(file) == os.fspath(denied):
            raise PermissionError(13, "denied", os.fspath(file))
        if os.fspath(file) == os.fspath(vanished):
            raise FileNotFoundError(os.fspath(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_with_failures)

    result = registry.scan_session_procs(max_age=0.0)

    assert result.records == ()
    assert result.complete is False
    assert len(result.issues) == 1
    assert result.issues[0].path == os.fspath(denied)
    assert "denied" in result.issues[0].detail


def test_scan_session_procs_file_race_alone_remains_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    vanished = tmp_path / "sessions" / "vanished.json"
    _write_json(vanished, {"pid": 2, "sessionId": "sid-vanished"})
    original_open = builtins.open

    def vanish(file, *args, **kwargs):
        if os.fspath(file) == os.fspath(vanished):
            raise FileNotFoundError(os.fspath(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", vanish)

    result = registry.scan_session_procs(max_age=0.0)

    assert result.records == ()
    assert result.complete is True
    assert result.issues == ()


def test_scan_session_procs_reports_unreadable_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    original_scandir = os.scandir

    def deny_root(target):
        if os.fspath(target) == os.fspath(sessions):
            raise PermissionError(13, "denied", os.fspath(target))
        return original_scandir(target)

    monkeypatch.setattr(registry.os, "scandir", deny_root)

    result = registry.scan_session_procs(max_age=0.0)

    assert len(result.issues) == 1
    assert result.complete is False
    assert result.issues[0].path == os.fspath(sessions)
    assert "denied" in result.issues[0].detail


def test_registry_scan_does_not_swallow_programming_typeerror(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    sessions = tmp_path / "sessions"
    _write_json(sessions / "1.json", {"pid": 1, "sessionId": "sid"})
    monkeypatch.setattr(
        registry.json,
        "load",
        lambda _stream: (_ for _ in ()).throw(TypeError("parser bug")),
    )

    with pytest.raises(TypeError, match="parser bug"):
        registry.scan_session_procs(max_age=0.0)


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


def test_host_pid_for_sid_never_alive_from_uninjected_rows():
    # Tri-state sentinel: raw registry rows (proc_alive=None) can never yield
    # alive=True — only liveness.live_session_procs injection can.
    procs = [_sp(100, "sid-a", None), _sp(101, "sid-a", None)]
    assert registry.host_pid_for_sid("sid-a", procs) == (100, False)


def test_host_pid_for_sid_none_when_unknown():
    assert registry.host_pid_for_sid("sid-missing", [_sp(100, "sid-a", True)]) == (
        None,
        False,
    )


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
