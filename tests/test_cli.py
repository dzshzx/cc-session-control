"""CLI wiring tests for `csctl prune` zombie/age sweeps (R7.1/R7.2).

The selection/exclusion logic itself is unit-tested in test_cleanup.py; these
tests verify the CLI surfaces those already-gated strategies (dry-run + apply +
the R10 refusal) so they are reachable by a user, not just by the library.
"""

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

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
    # under test don't depend on the session scan. `_cmd_prune`'s header now
    # builds the frozen `CleanupPlan` (via `build_plan`), which consults the
    # orphan protected-sid set (H1) and reaches `liveness.alive_map`, so stub
    # that too.
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

    assert cli._cmd_prune(_args(sweep_aged=True, apply=False)) == 0
    out = capsys.readouterr().out
    assert "Would sweep 1 aged" in out
    assert os.path.exists(old)  # dry run keeps it

    assert cli._cmd_prune(_args(sweep_aged=True, apply=True)) == 0
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

    assert cli._cmd_prune(_args(sweep_zombies=True, apply=True)) == 0
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

    assert cli._cmd_prune(_args(sweep_zombies=True, apply=True)) == 1
    out = capsys.readouterr().out
    assert "Refused" in out
    assert os.path.exists(os.path.join(sessions_dir, "1.json"))  # nothing removed


def test_prune_sweep_orphans_reports_real_success(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    orphan = tmp_path / "session-env" / "ghost"
    orphan.mkdir(parents=True)

    status = cli._cmd_prune(_args(sweep_orphans=True, apply=True))
    output = capsys.readouterr().out

    assert status == 0
    assert "Swept 1 orphan dir(s)." in output
    assert not orphan.exists()


def test_prune_sweep_orphans_refuses_without_proc(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    orphan = tmp_path / "session-env" / "ghost"
    orphan.mkdir(parents=True)
    monkeypatch.setattr(proc_mod, "current_determinable", lambda: False)

    status = cli._cmd_prune(_args(sweep_orphans=True, apply=True))
    output = capsys.readouterr().out

    assert status == 1
    assert "Refused" in output
    assert orphan.exists()


def test_prune_aged_partial_failure_is_visible_and_nonzero(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    _stub_scan(monkeypatch)
    root = tmp_path / "shell-snapshots"
    root.mkdir()
    good = root / "good.txt"
    bad = root / "bad.txt"
    good.write_text("good")
    bad.write_text("bad")
    stamp = time.time() - 40 * 86400
    os.utime(good, (stamp, stamp))
    os.utime(bad, (stamp, stamp))
    original_unlink = os.unlink

    def fail_one(path: str) -> None:
        if os.fspath(path) == os.fspath(bad):
            raise PermissionError("permission denied")
        original_unlink(path)

    monkeypatch.setattr(os, "unlink", fail_one)

    status = cli._cmd_prune(_args(sweep_aged=True, apply=True))
    output = capsys.readouterr().out

    assert status == 1
    assert "Partial sweep" in output
    assert "removed 1" in output
    assert "failed 1" in output
    assert "permission denied" in output
    assert not good.exists()
    assert bad.exists()


def test_prune_aged_missing_target_is_not_counted_as_swept(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    _stub_scan(monkeypatch)
    root = tmp_path / "shell-snapshots"
    root.mkdir()
    old = root / "old.txt"
    old.write_text("data")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))

    original_unlink = os.unlink

    def disappear(path: str) -> None:
        if os.fspath(path) == os.fspath(old):
            original_unlink(path)
            raise FileNotFoundError(path)
        original_unlink(path)

    monkeypatch.setattr(os, "unlink", disappear)

    status = cli._cmd_prune(_args(sweep_aged=True, apply=True))
    output = capsys.readouterr().out

    assert status == 0
    assert "already missing 1" in output
    assert "Swept 1 aged" not in output


def test_prune_main_propagates_cleanup_failure_exit_status(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_build_parser",
        lambda: types.SimpleNamespace(
            parse_args=lambda: _args(command="prune"),
            error=lambda message: None,
        ),
    )
    monkeypatch.setattr(cli, "_apply_global_flags", lambda args: None)
    monkeypatch.setattr(cli, "_cmd_prune", lambda args: 1)

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 1


def test_env_command_reports_ledger_failure_on_stderr_and_exits_nonzero(
    tmp_path,
    monkeypatch,
    capsys,
):
    from cc_session_control.data import rc

    monkeypatch.setattr(cfg, "config_dir", tmp_path / "config")
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    cfg.config_dir.mkdir()
    path = cfg.environments_ledger
    path.write_text('{"prefix":"session","key":"OLD"}\n')
    original_open = Path.open

    def deny_ledger(target, *args, **kwargs):
        if target == path:
            raise PermissionError("history denied")
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_ledger)
    monkeypatch.setattr(rc, "scan_servers", lambda: [])
    monkeypatch.setattr(sys, "argv", ["csctl", "env"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    captured = capsys.readouterr()
    assert stopped.value.code == 1
    assert "Current bridge environments: 0" in captured.out
    assert "ledger history incomplete" in captured.out
    assert "history denied" in captured.err
    assert "Warning:" in captured.err


def test_env_command_sends_recoverable_bad_line_warning_to_stderr(
    tmp_path,
    monkeypatch,
    capsys,
):
    from cc_session_control.data import rc

    monkeypatch.setattr(cfg, "config_dir", tmp_path / "config")
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    cfg.config_dir.mkdir()
    cfg.environments_ledger.write_text("{broken\n")
    monkeypatch.setattr(rc, "scan_servers", lambda: [])

    status = cli._cmd_env(types.SimpleNamespace())

    captured = capsys.readouterr()
    assert status == 0
    assert "ledger history incomplete" in captured.out
    assert "第 1 行" in captured.err
    assert "Warning:" in captured.err


def test_theme_flag_sets_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli._build_parser().parse_args(["--theme", "light"])
    cli._apply_global_flags(args)
    assert cfg.theme == "light"


def test_theme_flag_absent_keeps_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli._build_parser().parse_args([])
    cli._apply_global_flags(args)
    assert cfg.theme == "auto"


def test_rc_add_reports_unavailable_trust_without_calling_it_untrusted(
    tmp_path, monkeypatch, capsys,
):
    from cc_session_control.data import rc

    project = tmp_path / "app"
    project.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{broken")
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)

    with pytest.raises(SystemExit) as stopped:
        cli._cmd_rc(types.SimpleNamespace(rc_command="add", project=str(project)))

    output = capsys.readouterr().out
    assert stopped.value.code == 1
    assert "Project settings unavailable" in output
    assert "Not trusted" not in output
