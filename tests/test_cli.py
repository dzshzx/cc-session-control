"""CLI wiring tests for `csctl prune` zombie/age sweeps (R7.1/R7.2).

The selection/exclusion logic itself is unit-tested in test_cleanup.py; these
tests verify the CLI surfaces those already-gated strategies (dry-run + apply +
the R10 refusal) so they are reachable by a user, not just by the library.
"""

import json
import os
import subprocess
import time
import types
from pathlib import Path

from cc_session_control import cli, cli_commands, cli_rc
from cc_session_control.config import cfg
from cc_session_control.data import liveness, registry, sessions
from cc_session_control.data import proc as proc_mod
from cc_session_control.data.liveness import LivenessSnapshot
from cc_session_control.models import SessionProc


def _args(**kw):
    base = dict(
        max_prompts=0,
        apply=False,
        sweep_orphans=False,
        sweep_zombies=False,
        sweep_aged=False,
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
    monkeypatch.setattr(sessions, "scan", lambda inputs=None: [])
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(),
    )
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    registry.invalidate_cache()


def test_prune_default_dry_run_then_apply(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    monkeypatch.setattr(proc_mod, "current_determinable", lambda: True)

    assert cli.main(["prune"]) == 0
    output = capsys.readouterr().out
    assert "Would prune 0 session(s)" in output
    assert "Dry run" in output

    assert cli.main(["prune", "--apply"]) == 0
    output = capsys.readouterr().out
    assert "Would prune 0 session(s)" in output
    assert "No session(s) removed." in output


def test_prune_sweep_orphans_dry_run_preserves_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    orphan = tmp_path / "session-env" / "ghost"
    orphan.mkdir(parents=True)
    monkeypatch.setattr(proc_mod, "current_determinable", lambda: True)

    assert cli.main(["prune", "--sweep-orphans"]) == 0
    output = capsys.readouterr().out
    assert "Would sweep 1 orphan artifact dir(s)" in output
    assert "Dry run" in output
    assert orphan.exists()


def test_prune_sweep_zombies_dry_run_preserves_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    target = sessions_dir / "4242.json"
    target.write_text("{}")
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(
            session_procs=(
                SessionProc(
                    pid=4242,
                    sid="dead",
                    proc_alive=False,
                ),
            ),
        ),
    )
    monkeypatch.setattr(proc_mod, "current_determinable", lambda: True)

    assert cli.main(["prune", "--sweep-zombies"]) == 0
    output = capsys.readouterr().out
    assert "Would sweep 1 zombie session file(s)" in output
    assert "Dry run" in output
    assert target.exists()


def test_prune_sweep_aged_dry_run_then_apply(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    _stub_scan(monkeypatch)
    snap = _mkdir(tmp_path, "shell-snapshots")
    old = os.path.join(snap, "old.sh")
    open(old, "w").close()
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))

    assert cli_commands.handle_prune(_args(sweep_aged=True, apply=False)) == 0
    out = capsys.readouterr().out
    assert "Would sweep 1 aged" in out
    assert os.path.exists(old)  # dry run keeps it

    assert cli_commands.handle_prune(_args(sweep_aged=True, apply=True)) == 0
    out = capsys.readouterr().out
    assert "Swept 1 aged" in out
    assert not os.path.exists(old)


def test_prune_sweep_zombies_apply_keeps_alive_and_current(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    sessions_dir = _mkdir(tmp_path, "sessions")
    for pid in (700772, 710575):
        with open(os.path.join(sessions_dir, f"{pid}.json"), "w") as fh:
            json.dump({"pid": pid, "sessionId": "A", "procStart": str(pid)}, fh)

    monkeypatch.setattr(proc_mod, "current_determinable", lambda: True)
    monkeypatch.setattr(proc_mod, "ancestor_pids", lambda: set())
    monkeypatch.setattr(proc_mod, "pid_alive", lambda pid, ps: pid == 710575)
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(
            session_procs=(
                SessionProc(
                    pid=700772,
                    sid="A",
                    proc_start="700772",
                    proc_alive=False,
                ),
                SessionProc(
                    pid=710575,
                    sid="A",
                    proc_start="710575",
                    proc_alive=True,
                ),
            ),
        ),
    )

    assert cli_commands.handle_prune(_args(sweep_zombies=True, apply=True)) == 0
    out = capsys.readouterr().out
    assert "Swept 1 zombie" in out
    assert not os.path.exists(os.path.join(sessions_dir, "700772.json"))  # dead
    assert os.path.exists(os.path.join(sessions_dir, "710575.json"))  # alive kept


def test_prune_sweep_zombies_refuses_without_proc(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    sessions_dir = _mkdir(tmp_path, "sessions")
    with open(os.path.join(sessions_dir, "1.json"), "w") as fh:
        json.dump({"pid": 1, "sessionId": "A", "procStart": "1"}, fh)

    issue = proc_mod.ProcIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        proc_mod,
        "probe_current_ancestors",
        lambda: proc_mod.AncestorProbe(frozenset(), (issue,)),
    )

    assert cli_commands.handle_prune(_args(sweep_zombies=True, apply=True)) == 1
    captured = capsys.readouterr()
    assert "Refused" in captured.err
    assert os.path.exists(os.path.join(sessions_dir, "1.json"))  # nothing removed


def test_prune_sweep_orphans_reports_real_success(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    orphan = tmp_path / "session-env" / "ghost"
    orphan.mkdir(parents=True)

    status = cli_commands.handle_prune(_args(sweep_orphans=True, apply=True))
    output = capsys.readouterr().out

    assert status == 0
    assert "Swept 1 orphan dir(s)." in output
    assert not orphan.exists()


def test_prune_sweep_orphans_refuses_without_proc(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    orphan = tmp_path / "session-env" / "ghost"
    orphan.mkdir(parents=True)
    issue = proc_mod.ProcIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        proc_mod,
        "probe_current_ancestors",
        lambda: proc_mod.AncestorProbe(frozenset(), (issue,)),
    )

    status = cli_commands.handle_prune(_args(sweep_orphans=True, apply=True))
    captured = capsys.readouterr()

    assert status == 1
    assert "Refused" in captured.err
    assert orphan.exists()


def test_prune_apply_reports_incomplete_liveness_and_preserves_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    orphan = tmp_path / "session-env" / "ghost"
    orphan.mkdir(parents=True)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    malformed = sessions_dir / "broken.json"
    malformed.write_text("{bad json")
    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(proc_mod, "current_determinable", lambda: True)
    monkeypatch.setattr(proc_mod, "ancestor_pids", lambda: set())
    registry.invalidate_cache()
    liveness.invalidate_cache()

    status = cli_commands.handle_prune(_args(sweep_orphans=True, apply=True))
    captured = capsys.readouterr()

    assert status == 1
    assert "Liveness evidence incomplete; nothing deleted" in captured.err
    assert "session registry" in captured.err
    assert os.fspath(malformed) in captured.err
    assert orphan.exists()


def test_prune_aged_partial_failure_is_visible_and_nonzero(
    tmp_path,
    monkeypatch,
    capsys,
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

    def fail_one(path: str, *, dir_fd: int | None = None) -> None:
        if os.fspath(path) == bad.name:
            raise PermissionError("permission denied")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", fail_one)

    status = cli_commands.handle_prune(_args(sweep_aged=True, apply=True))
    captured = capsys.readouterr()

    assert status == 1
    assert "Partial sweep" in captured.err
    assert "removed 1" in captured.err
    assert "failed 1" in captured.err
    assert "permission denied" in captured.err
    assert not good.exists()
    assert bad.exists()


def test_prune_aged_missing_target_is_not_counted_as_swept(
    tmp_path,
    monkeypatch,
    capsys,
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

    def disappear(path: str, *, dir_fd: int | None = None) -> None:
        if os.fspath(path) == old.name:
            original_unlink(path, dir_fd=dir_fd)
            raise FileNotFoundError(path)
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", disappear)

    status = cli_commands.handle_prune(_args(sweep_aged=True, apply=True))
    output = capsys.readouterr().out

    assert status == 0
    assert "already missing 1" in output
    assert "Swept 1 aged" not in output


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
    status = cli.main(["env"])

    captured = capsys.readouterr()
    assert status == 1
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

    status = cli_commands.handle_env(types.SimpleNamespace())

    captured = capsys.readouterr()
    assert status == 0
    assert "ledger history incomplete" in captured.out
    assert "第 1 行" in captured.err
    assert "Warning:" in captured.err


def test_theme_flag_sets_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli.build_parser().parse_args(["--theme", "light"])
    cli.apply_global_flags(args)
    assert cfg.theme == "light"


def test_theme_flag_absent_keeps_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli.build_parser().parse_args([])
    cli.apply_global_flags(args)
    assert cfg.theme == "auto"


def test_rc_add_reports_unavailable_trust_without_calling_it_untrusted(
    tmp_path,
    monkeypatch,
    capsys,
):
    from cc_session_control.data import rc

    project = tmp_path / "app"
    project.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{broken")
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)

    status = cli_rc.handle_add(
        types.SimpleNamespace(rc_command="add", project=str(project)),
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "Project settings unavailable" in captured.err
    assert "Not trusted" not in captured.err


def test_tui_exit_intent_runs_only_after_main_loop_returns(monkeypatch):
    from cc_session_control import app as app_mod
    from cc_session_control.actions.session_ops import ExitIntent

    events = []

    class Intent(ExitIntent):
        def run(self) -> int:
            events.append("intent")
            return 0

    intent = Intent()

    class FakeApp:
        def run(self):
            events.append("loop")
            return intent

    monkeypatch.setattr(app_mod, "App", FakeApp)

    assert cli_commands.handle_tui(types.SimpleNamespace()) == 0

    assert events == ["loop", "intent"]
