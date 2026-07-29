"""CLI wiring tests for `csctl prune` zombie/age sweeps (R7.1/R7.2).

The selection/exclusion logic itself is unit-tested in test_cleanup.py; these
tests verify the CLI surfaces those already-gated strategies (dry-run + apply +
the R10 refusal) so they are reachable by a user, not just by the library.
"""

import io
import json
import os
import subprocess
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest

from cc_session_control import cli, cli_commands, cli_rc
from cc_session_control.actions import session_ops
from cc_session_control.config import cfg
from cc_session_control.data import age_cleanup, cleanup, liveness, registry, sessions
from cc_session_control.data import proc as proc_mod
from cc_session_control.data.liveness import LivenessSnapshot
from cc_session_control.models import Session, SessionProc


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
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda inputs=None: sessions.SessionScanResult(),
    )
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(),
    )
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    registry.invalidate_cache()


def test_prune_refuses_incomplete_transcript_preview(monkeypatch, capsys):
    _stub_scan(monkeypatch)
    issue = sessions.TranscriptIssue(
        "session transcript",
        "/runtime/projects/project/session.jsonl",
        "permission denied",
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda inputs=None: sessions.SessionScanResult(issues=(issue,)),
    )

    assert cli.main(["prune"]) == 1
    captured = capsys.readouterr()
    assert "Total:" not in captured.out
    assert "Would prune" not in captured.out
    assert "Refused: transcript evidence is incomplete" in captured.err
    assert "/runtime/projects/project/session.jsonl" in captured.err
    assert "permission denied" in captured.err


def test_prune_refuses_incomplete_liveness_before_preview(monkeypatch, capsys):
    issue = liveness.LivenessIssue(
        "process ancestors",
        "/proc",
        "permission denied",
    )
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(issues=(issue,)),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: (_ for _ in ()).throw(
            AssertionError("incomplete liveness must stop before transcript planning")
        ),
    )

    assert cli_commands.handle_prune(_args()) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Refused: liveness evidence is incomplete" in captured.err
    assert "process ancestors (/proc): permission denied" in captured.err


def test_prune_orphan_apply_reports_fresh_incomplete_transcript_and_preserves_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    target = cfg.session_env_dir / "ghost"
    target.mkdir(parents=True)
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(),
    )
    monkeypatch.setattr(
        proc_mod,
        "probe_current_ancestors",
        lambda: proc_mod.AncestorProbe(frozenset()),
    )
    issue = sessions.TranscriptIssue(
        "session transcript",
        "/runtime/projects/project/ghost.jsonl",
        "permission denied",
    )
    scans = iter(
        (
            sessions.SessionScanResult(),
            sessions.SessionScanResult(issues=(issue,)),
        )
    )
    monkeypatch.setattr(sessions, "scan_result", lambda inputs=None: next(scans))

    assert cli.main(["prune", "--sweep-orphans", "--apply"]) == 1
    captured = capsys.readouterr()
    assert target.is_dir()
    assert "Swept" not in captured.out
    assert "Refused: no orphan dir(s) removed" in captured.err
    assert "/runtime/projects/project/ghost.jsonl" in captured.err
    assert "permission denied" in captured.err


def test_prune_default_dry_run_then_apply(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    monkeypatch.setattr(
        proc_mod,
        "probe_current_ancestors",
        lambda: (_ for _ in ()).throw(
            AssertionError("default preview must use the typed generation")
        ),
    )

    assert cli.main(["prune"]) == 0
    output = capsys.readouterr().out
    assert "Would prune 0 session(s)" in output
    assert "Dry run" in output

    assert cli.main(["prune", "--apply"]) == 0
    output = capsys.readouterr().out
    assert "Would prune 0 session(s)" in output
    assert "No session(s) removed." in output


def test_prune_preview_uses_one_typed_generation_without_ancestor_reprobe(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    session = Session(
        sid="generation-candidate",
        cwd="/project",
        label="candidate",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=os.fspath(tmp_path / "projects" / "candidate.jsonl"),
    )
    evidence = LivenessSnapshot()
    liveness_calls = 0

    def read_liveness():
        nonlocal liveness_calls
        liveness_calls += 1
        return evidence

    monkeypatch.setattr(liveness, "liveness_inputs", read_liveness)
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda inputs: (
            sessions.SessionScanResult((session,))
            if inputs is evidence
            else (_ for _ in ()).throw(AssertionError("wrong generation evidence"))
        ),
    )
    monkeypatch.setattr(
        proc_mod,
        "probe_current_ancestors",
        lambda: (_ for _ in ()).throw(
            AssertionError("preview must not reprobe ancestors")
        ),
    )

    assert cli_commands.handle_prune(_args()) == 0
    captured = capsys.readouterr()
    assert liveness_calls == 1
    assert "Would prune 1 session(s)" in captured.out
    assert captured.err == ""


def test_prune_sweep_orphans_dry_run_preserves_target(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _stub_scan(monkeypatch)
    orphan = tmp_path / "session-env" / "ghost"
    orphan.mkdir(parents=True)
    monkeypatch.setattr(
        proc_mod,
        "probe_current_ancestors",
        lambda: (_ for _ in ()).throw(
            AssertionError("orphan preview must use the typed generation")
        ),
    )

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
    monkeypatch.setattr(
        proc_mod,
        "probe_current_ancestors",
        lambda: (_ for _ in ()).throw(
            AssertionError("zombie preview must use the typed generation")
        ),
    )

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


def test_prune_age_only_dry_run_and_apply_skip_session_protection(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    root = tmp_path / "shell-snapshots"
    root.mkdir()
    old = root / "old.sh"
    recent = root / "recent.sh"
    old.write_text("old")
    recent.write_text("recent")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))

    sources = {
        "liveness": (liveness, "liveness_inputs"),
        "transcripts": (sessions, "scan_result"),
        "ancestors": (proc_mod, "probe_current_ancestors"),
        "cleanup plan": (cleanup, "build_plan"),
        "session registry": (registry, "scan_session_procs"),
        "agent registry": (registry, "scan_agent_jobs"),
        "agents CLI": (liveness, "scan_agents"),
    }
    calls = dict.fromkeys(sources, 0)

    def bomb(source):
        def fail(*_args, **_kwargs):
            calls[source] += 1
            raise AssertionError(f"age-only prune accessed {source}")

        return fail

    for source, (module, attribute) in sources.items():
        monkeypatch.setattr(module, attribute, bomb(source))

    assert cli.main(["prune", "--sweep-aged"]) == 0
    captured = capsys.readouterr()

    assert calls == dict.fromkeys(sources, 0)
    assert "Would sweep 1 aged" in captured.out
    assert "Dry run" in captured.out
    assert captured.err == ""
    assert old.exists()
    assert recent.exists()

    assert cli.main(["prune", "--sweep-aged", "--apply"]) == 0
    captured = capsys.readouterr()

    assert calls == dict.fromkeys(sources, 0)
    assert "Swept 1 aged" in captured.out
    assert captured.err == ""
    assert not old.exists()
    assert recent.exists()


def test_prune_age_only_apply_refuses_source_root_replaced_after_preview(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    root = tmp_path / "shell-snapshots"
    root.mkdir()
    old = root / "old.sh"
    old.write_text("preview object")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))
    saved_root = tmp_path / "saved-shell-snapshots"
    replacement = root / "old.sh"

    class ReplaceRootOnPreview(io.StringIO):
        replaced = False

        def write(self, text):
            if not self.replaced and "Would sweep 1 aged" in text:
                root.rename(saved_root)
                root.mkdir()
                replacement.write_text("replacement object")
                os.utime(replacement, (stamp, stamp))
                self.replaced = True
            return super().write(text)

    stdout = ReplaceRootOnPreview()
    stderr = io.StringIO()

    status = cli_commands.handle_prune(
        _args(sweep_aged=True, apply=True),
        stdout=stdout,
        stderr=stderr,
    )

    assert stdout.replaced
    assert status == 1
    assert "Refused" in stderr.getvalue()
    assert "root" in stderr.getvalue()
    assert replacement.read_text() == "replacement object"
    assert (saved_root / "old.sh").read_text() == "preview object"


def test_prune_age_only_surfaces_inventory_failure_for_dry_run_and_apply(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    source = tmp_path / "shell-snapshots"
    source.mkdir()
    original_listdir = os.listdir

    def deny_source(path):
        if os.fspath(path) == os.fspath(source):
            raise PermissionError(13, "permission denied", os.fspath(path))
        return original_listdir(path)

    monkeypatch.setattr(age_cleanup.os, "listdir", deny_source)

    for apply in (False, True):
        status = cli_commands.handle_prune(
            _args(sweep_aged=True, apply=apply),
        )
        captured = capsys.readouterr()

        assert status == 1
        assert "Warning: cleanup preview is partial: aged_entries" in captured.err
        assert os.fspath(source) in captured.err
        assert "permission denied" in captured.err
        assert "Traceback" not in captured.err
        assert "Would sweep 0 aged" in captured.out


def test_prune_orphan_and_aged_flags_keep_orphan_evidence_precedence(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    root = tmp_path / "shell-snapshots"
    root.mkdir()
    old = root / "old.sh"
    old.write_text("old")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))
    issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
    liveness_calls = 0

    def incomplete_liveness():
        nonlocal liveness_calls
        liveness_calls += 1
        return LivenessSnapshot(issues=(issue,))

    monkeypatch.setattr(liveness, "liveness_inputs", incomplete_liveness)
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: (_ for _ in ()).throw(
            AssertionError("incomplete liveness must stop transcript planning")
        ),
    )

    assert (
        cli.main(
            ["prune", "--sweep-orphans", "--sweep-aged", "--apply"],
        )
        == 1
    )
    captured = capsys.readouterr()

    assert liveness_calls == 1
    assert "Refused: liveness evidence is incomplete" in captured.err
    assert "Would sweep" not in captured.out
    assert old.exists()


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

    issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(issues=(issue,)),
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
    issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: LivenessSnapshot(issues=(issue,)),
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
    assert "Refused: liveness evidence is incomplete" in captured.err
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
    bad_inode = os.stat(bad, follow_symlinks=False).st_ino

    def fail_one(path: str, *, dir_fd: int | None = None) -> None:
        metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if metadata.st_ino == bad_inode:
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
    old_inode = os.stat(old, follow_symlinks=False).st_ino

    def disappear(path: str, *, dir_fd: int | None = None) -> None:
        metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if metadata.st_ino == old_inode:
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


def test_env_command_reports_partial_ledger_as_blocked_and_preserves_bytes(
    tmp_path,
    monkeypatch,
    capsys,
):
    from cc_session_control.data import rc

    monkeypatch.setattr(cfg, "config_dir", tmp_path / "config")
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    cfg.config_dir.mkdir()
    cfg.environments_ledger.write_text("{broken\n")
    original = cfg.environments_ledger.read_bytes()
    monkeypatch.setattr(rc, "scan_servers", lambda: [])

    status = cli_commands.handle_env(types.SimpleNamespace())

    captured = capsys.readouterr()
    assert status == 1
    assert "ledger history incomplete" in captured.out
    assert "第 1 行" in captured.err
    assert "已保留原文件并停止更新，孤儿历史不可用" in captured.err
    assert "Warning:" in captured.err
    assert cfg.environments_ledger.read_bytes() == original


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


def _takeover_session(tmp_path, **changes):
    session = Session(
        sid="stable-sid",
        cwd=str(tmp_path),
        label="target",
        mtime=1,
        prompts=1,
        pid=None,
        alive=False,
        current=False,
    )
    return replace(session, **changes)


def _install_takeover_rows(monkeypatch, rows):
    monkeypatch.setattr(liveness, "liveness_inputs", lambda: LivenessSnapshot())
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(rows),
    )


def test_resume_take_over_re_resolves_execution_time_identity(
    tmp_path, monkeypatch, capsys
):
    displayed = _takeover_session(
        tmp_path, cwd="/display-time", pid=4242, proc_start="old", alive=True
    )
    assert session_ops.resume_cmd(displayed) == "csctl resume --take-over stable-sid"
    target = replace(displayed, cwd=str(tmp_path), pid=9002, proc_start="new-start")
    _install_takeover_rows(monkeypatch, (target,))
    monkeypatch.setattr(
        session_ops.proc,
        "probe_current_ancestors",
        lambda: session_ops.proc.AncestorProbe(frozenset({111})),
    )
    probes, killed, changed, executed = [], [], [], []
    monkeypatch.setattr(
        session_ops.proc,
        "probe_pid",
        lambda pid, start: (
            probes.append((pid, start)) or session_ops.proc.PidProbe(pid, True)
        ),
    )
    monkeypatch.setattr(session_ops.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(session_ops.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(session_ops.os, "chdir", lambda cwd: changed.append(cwd))
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda program, argv: executed.append((program, argv)),
    )

    assert cli.main(["resume", "--take-over", "stable-sid"]) == 0
    assert capsys.readouterr().err == ""
    assert probes == [(9002, "new-start")]
    assert killed == [9002]
    assert changed == [str(tmp_path)]
    assert executed == [("claude", ["claude", "--resume", "stable-sid"])]


@pytest.mark.parametrize("unsafe_state", ["recycled", "ancestor"])
def test_resume_take_over_never_kills_recycled_or_current_ancestor_pid(
    unsafe_state, tmp_path, monkeypatch, capsys
):
    target = _takeover_session(
        tmp_path, pid=9002, proc_start="expected-start", alive=True
    )
    _install_takeover_rows(monkeypatch, (target,))
    ancestors = {9002} if unsafe_state == "ancestor" else {111}
    monkeypatch.setattr(
        session_ops.proc,
        "probe_current_ancestors",
        lambda: session_ops.proc.AncestorProbe(frozenset(ancestors)),
    )
    monkeypatch.setattr(
        session_ops.proc,
        "probe_pid",
        lambda pid, _start: session_ops.proc.PidProbe(pid, False),
    )
    monkeypatch.setattr(
        session_ops.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not kill")),
    )
    executed = []
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda program, argv: executed.append((program, argv)),
    )

    status = cli.main(["resume", "--take-over", "stable-sid"])
    captured = capsys.readouterr()
    if unsafe_state == "ancestor":
        assert status == 1
        assert "current session ancestor chain" in captured.err
        assert executed == []
    else:
        assert status == 0
        assert executed == [("claude", ["claude", "--resume", "stable-sid"])]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", "missing"),
        ("ambiguous", "ambiguous"),
        ("current", "current"),
        ("pid", "pid"),
        ("proc_start", "proc_start"),
        ("liveness", "liveness evidence is incomplete"),
        ("transcript", "transcript inventory is incomplete"),
    ],
)
def test_resume_take_over_refuses_unsafe_target_before_kill_or_exec(
    case, expected, tmp_path, monkeypatch, capsys
):
    target = _takeover_session(tmp_path)
    rows = (target,)
    if case == "missing":
        rows = ()
    elif case == "ambiguous":
        rows = (target, replace(target, cwd=str(tmp_path / "other")))
    elif case == "current":
        rows = (replace(target, alive=True, current=True, pid=9002),)
    elif case == "pid":
        rows = (replace(target, alive=True, proc_start="known"),)
    elif case == "proc_start":
        rows = (replace(target, alive=True, pid=9002),)

    if case == "liveness":
        issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
        monkeypatch.setattr(
            liveness,
            "liveness_inputs",
            lambda: LivenessSnapshot(issues=(issue,)),
        )
        monkeypatch.setattr(
            sessions,
            "scan_result",
            lambda _inputs: (_ for _ in ()).throw(AssertionError("must not scan")),
        )
    else:
        _install_takeover_rows(monkeypatch, rows)
        if case == "transcript":
            issue = sessions.TranscriptIssue("session transcript", "/x", "unreadable")
            monkeypatch.setattr(
                sessions,
                "scan_result",
                lambda _inputs: sessions.SessionScanResult(rows, (issue,)),
            )
    for boundary in ("kill", "execvp"):
        monkeypatch.setattr(
            session_ops.os,
            boundary,
            lambda *_args: (_ for _ in ()).throw(AssertionError("unsafe boundary")),
        )

    assert cli.main(["resume", "--take-over", "stable-sid"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err


def test_resume_take_over_exec_failure_is_contextual(tmp_path, monkeypatch, capsys):
    _install_takeover_rows(monkeypatch, (_takeover_session(tmp_path),))
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(OSError("exec denied")),
    )

    assert cli.main(["resume", "--take-over", "stable-sid"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to take over session stable-sid" in captured.err
    assert "exec denied" in captured.err
