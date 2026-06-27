"""CLI wiring tests for `csctl prune` zombie/age sweeps (R7.1/R7.2).

The selection/exclusion logic itself is unit-tested in test_cleanup.py; these
tests verify the CLI surfaces those already-gated strategies (dry-run + apply +
the R10 refusal) so they are reachable by a user, not just by the library.
"""

import json
import os
import time
import types

from cc_session_control import cli
from cc_session_control.config import cfg
from cc_session_control.data import liveness
from cc_session_control.data import proc as proc_mod
from cc_session_control.data import registry, sessions


def _args(**kw):
    base = dict(
        max_prompts=0, apply=False, sweep_orphans=False,
        sweep_zombies=False, sweep_aged=False,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _mkdir(base, *parts):
    d = os.path.join(str(base), *parts)
    os.makedirs(d, exist_ok=True)
    return d


def _stub_scan(monkeypatch):
    # Avoid the transcript glob + `claude agents --json` subprocess; the sweeps
    # under test don't depend on the session scan. `cleanup_stats` (called by
    # `_cmd_prune`) now consults the orphan protected-sid set (H1), which reaches
    # `liveness.alive_map`, so stub that too.
    monkeypatch.setattr(sessions, "scan", lambda: [])
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    registry.invalidate_cache()


def test_prune_sweep_aged_dry_run_then_apply(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    _stub_scan(monkeypatch)
    snap = _mkdir(tmp_path, "shell-snapshots")
    old = os.path.join(snap, "old.sh")
    open(old, "w").close()
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))

    cli._cmd_prune(_args(sweep_aged=True, apply=False))
    out = capsys.readouterr().out
    assert "Would sweep 1 aged" in out
    assert os.path.exists(old)  # dry run keeps it

    cli._cmd_prune(_args(sweep_aged=True, apply=True))
    out = capsys.readouterr().out
    assert "Swept 1 aged" in out
    assert not os.path.exists(old)


def test_prune_sweep_zombies_apply_keeps_alive_and_current(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    sessions_dir = _mkdir(tmp_path, "sessions")
    for pid in (700772, 710575):
        with open(os.path.join(sessions_dir, f"{pid}.json"), "w") as fh:
            json.dump({"pid": pid, "sessionId": "A", "procStart": str(pid)}, fh)

    monkeypatch.setattr(proc_mod, "current_determinable", lambda: True)
    monkeypatch.setattr(proc_mod, "ancestor_pids", lambda: set())
    monkeypatch.setattr(proc_mod, "pid_alive", lambda pid, ps: pid == 710575)

    cli._cmd_prune(_args(sweep_zombies=True, apply=True))
    out = capsys.readouterr().out
    assert "Swept 1 zombie" in out
    assert not os.path.exists(os.path.join(sessions_dir, "700772.json"))  # dead
    assert os.path.exists(os.path.join(sessions_dir, "710575.json"))      # alive kept


def test_prune_sweep_zombies_refuses_without_proc(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    sessions_dir = _mkdir(tmp_path, "sessions")
    with open(os.path.join(sessions_dir, "1.json"), "w") as fh:
        json.dump({"pid": 1, "sessionId": "A", "procStart": "1"}, fh)

    monkeypatch.setattr(proc_mod, "current_determinable", lambda: False)

    cli._cmd_prune(_args(sweep_zombies=True, apply=True))
    out = capsys.readouterr().out
    assert "Refused" in out
    assert os.path.exists(os.path.join(sessions_dir, "1.json"))  # nothing removed
