"""Filesystem-boundary tests for truthful cleanup removal results."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from cc_session_control.config import cfg
from cc_session_control.data import (
    cleanup,
    cleanup_liveness,
    liveness,
    registry,
    removal,
)
from cc_session_control.data.removal import RemovalStatus, remove_path
from cc_session_control.models import Session, SessionProc


def test_remove_path_refuses_without_atomic_rename_capability(tmp_path, monkeypatch):
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    monkeypatch.setattr(removal, "_renameat2", None)

    result = remove_path(target)

    assert result.status is RemovalStatus.REFUSED
    assert "renameat2(RENAME_NOREPLACE)" in (result.error or "")
    assert target.read_text() == "keep"


def test_remove_directory_reports_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "locked"
    target.mkdir()

    def refuse(path: str, *, dir_fd: int | None = None) -> None:
        raise PermissionError("read-only filesystem")

    refuse.avoids_symlink_attacks = True
    monkeypatch.setattr(shutil, "rmtree", refuse)

    result = remove_path(target)

    assert result.status is RemovalStatus.FAILED
    assert result.path == target
    assert "read-only filesystem" in (result.error or "")
    assert target.exists()


def test_remove_directory_symlink_does_not_follow_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    payload = target / "keep.txt"
    payload.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    result = remove_path(link)

    assert result.status is RemovalStatus.REMOVED
    assert not link.exists()
    assert payload.read_text() == "keep"


def test_remove_path_reports_missing_when_it_disappears_during_delete(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "racing.txt"
    target.write_text("data")
    original_unlink = os.unlink

    def disappear(path: str, *, dir_fd: int | None = None) -> None:
        original_unlink(path, dir_fd=dir_fd)
        raise FileNotFoundError(path)

    monkeypatch.setattr(os, "unlink", disappear)

    result = remove_path(target)

    assert result.status is RemovalStatus.MISSING
    assert result.path == target
    assert not target.exists()


def test_aged_execution_reports_partial_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    root = tmp_path / "shell-snapshots"
    good = root / "good"
    bad = root / "bad"
    good.mkdir(parents=True)
    bad.mkdir()
    stamp = time.time() - 40 * 86400
    os.utime(good, (stamp, stamp))
    os.utime(bad, (stamp, stamp))
    original_rmtree = shutil.rmtree
    bad_inode = os.stat(bad, follow_symlinks=False).st_ino

    def fail_one(path: str, *, dir_fd: int | None = None) -> None:
        metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if metadata.st_ino == bad_inode:
            raise PermissionError("permission denied")
        original_rmtree(path, dir_fd=dir_fd)

    fail_one.avoids_symlink_attacks = True
    monkeypatch.setattr(shutil, "rmtree", fail_one)

    result = cleanup.execute_aged_removals(
        ["shell-snapshots/good", "shell-snapshots/bad"],
    )

    assert [item.path for item in result.removed] == [good]
    assert [item.path for item in result.failed] == [bad]
    assert result.failed[0].error == "permission denied"
    assert not good.exists()
    assert bad.exists()


def test_duplicate_execution_target_is_removed_once_then_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    root = tmp_path / "shell-snapshots"
    root.mkdir()
    old = root / "old.txt"
    old.write_text("data")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))

    result = cleanup.execute_aged_removals(
        ["shell-snapshots/old.txt", "shell-snapshots/old.txt"],
    )

    assert [item.path for item in result.removed] == [old]
    assert [item.path for item in result.missing] == [old]


def test_build_plan_keeps_partial_results_and_reports_source_issue(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    old = tmp_path / "shell-snapshots" / "old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("data")
    stamp = time.time() - 40 * 86400
    os.utime(old, (stamp, stamp))
    (tmp_path / "session-env").mkdir()
    original_listdir = os.listdir

    def fail_one_source(path: str) -> list[str]:
        if os.fspath(path) == os.fspath(tmp_path / "session-env"):
            raise PermissionError("cannot read artifacts")
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", fail_one_source)

    plan = cleanup.build_plan(
        [],
        liveness.LivenessSnapshot(
            session_procs=(SessionProc(pid=7, sid="dead", proc_alive=False),),
        ),
        transcript_sids=frozenset(),
    )

    assert plan.aged_entries == ("shell-snapshots/old.txt",)
    assert plan.zombie_pids == (7,)
    assert len(plan.issues) == 1
    assert plan.issues[0].source == "orphan_dirs"
    assert "cannot read artifacts" in plan.issues[0].error


def test_build_plan_does_not_swallow_programming_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    (tmp_path / "session-env").mkdir()
    original_listdir = os.listdir

    def broken_invariant(path: str) -> list[str]:
        if os.fspath(path) == os.fspath(tmp_path / "session-env"):
            raise RuntimeError("broken invariant")
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", broken_invariant)

    try:
        cleanup.build_plan([], liveness.LivenessSnapshot(), transcript_sids=frozenset())
    except RuntimeError as exc:
        assert str(exc) == "broken invariant"
    else:
        raise AssertionError("programming error was swallowed")


def test_orphan_execution_reports_removed_and_failed_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        cleanup,
        "fresh_liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        cleanup.transcripts,
        "load_inventory",
        lambda _root: cleanup.transcripts.TranscriptInventory(),
    )
    good = tmp_path / "session-env" / "good"
    bad = tmp_path / "session-env" / "bad"
    good.mkdir(parents=True)
    bad.mkdir()
    original_rmtree = shutil.rmtree
    bad_inode = os.stat(bad, follow_symlinks=False).st_ino

    def fail_one(path: str, *, dir_fd: int | None = None) -> None:
        metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if metadata.st_ino == bad_inode:
            raise PermissionError("permission denied")
        original_rmtree(path, dir_fd=dir_fd)

    fail_one.avoids_symlink_attacks = True
    monkeypatch.setattr(shutil, "rmtree", fail_one)

    result = cleanup.execute_orphan_removals(["session-env/good", "session-env/bad"])

    assert [item.path for item in result.removed] == [good]
    assert [item.path for item in result.failed] == [bad]


def test_zombie_execution_reports_proc_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    target = tmp_path / "sessions" / "7.json"
    target.parent.mkdir()
    target.write_text("{}")
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(
            frozenset(),
            (cleanup.proc.ProcIssue("process ancestors", "/proc", "unavailable"),),
        ),
    )

    result = cleanup.execute_zombie_removals([7])

    assert [notice.target for notice in result.refused] == ["7"]
    assert not result.removed
    assert target.exists()


def test_remove_session_rechecks_liveness_before_deleting(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    transcript = tmp_path / "projects" / "p" / "sid-live.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}")
    target = Session(
        sid="sid-live",
        cwd="/tmp/p",
        label="live",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=str(transcript),
    )
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(cleanup.proc, "ancestor_pids", lambda: set())
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(
            session_procs=(SessionProc(pid=88, sid="sid-live", proc_alive=True),),
        ),
    )

    result = cleanup.remove_session(target)

    assert [notice.target for notice in result.skipped] == ["sid-live"]
    assert not result.removed
    assert transcript.exists()


def test_remove_session_retains_proc_issue_without_deleting(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    transcript = tmp_path / "projects" / "p" / "sid-unknown.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}")
    target = Session(
        sid="sid-unknown",
        cwd="/tmp/p",
        label="unknown",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=str(transcript),
    )
    issue = liveness.LivenessIssue(
        "process stat",
        "/proc/88/stat",
        "permission denied",
    )
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(
            frozenset(),
            (cleanup.proc.ProcIssue("process ancestors", "/proc", "unavailable"),),
        ),
    )
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(issues=(issue,)),
    )
    monkeypatch.setattr(
        cleanup,
        "remove_anchored",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not delete")),
    )

    result = cleanup.remove_session(target)

    assert [item.source for item in result.issues] == ["process stat"]
    assert result.issues[0].path == "/proc/88/stat"
    assert "permission denied" in result.issues[0].error
    assert transcript.exists()


def test_execution_rejects_target_outside_preview_directory(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    outside = tmp_path / "outside"
    outside.mkdir()
    stamp = time.time() - 40 * 86400
    os.utime(outside, (stamp, stamp))

    result = cleanup.execute_aged_removals(
        ["shell-snapshots/../outside"],
    )

    assert len(result.skipped) == 1
    assert not result.removed
    assert outside.exists()


def test_session_execution_refuses_visible_liveness_io_failure(
    tmp_path,
    monkeypatch,
):
    transcript = tmp_path / "sid.jsonl"
    transcript.write_text("{}")
    target = Session(
        sid="sid",
        cwd="/tmp/p",
        label="session",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=str(transcript),
    )
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(
            issues=(
                liveness.LivenessIssue(
                    "session registry",
                    "/runtime/sessions",
                    "cannot scan liveness",
                ),
            )
        ),
    )

    result = cleanup.execute_session_removals([target])

    assert len(result.refused) == 1
    assert "liveness evidence incomplete" in result.refused[0].reason
    assert "cannot scan liveness" in result.issues[0].error
    assert transcript.exists()


def test_session_execution_does_not_swallow_liveness_programming_error(
    tmp_path,
    monkeypatch,
):
    target = Session(
        sid="sid",
        cwd="/tmp/p",
        label="session",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=str(tmp_path / "sid.jsonl"),
    )
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(RuntimeError("broken liveness invariant")),
    )

    try:
        cleanup.execute_session_removals([target])
    except RuntimeError as exc:
        assert str(exc) == "broken liveness invariant"
    else:
        raise AssertionError("programming error was swallowed")


def test_session_execution_refuses_real_malformed_registry_before_removal(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(cleanup.proc, "ancestor_pids", lambda: set())
    transcript = tmp_path / "sid.jsonl"
    transcript.write_text("{}")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    malformed = sessions_dir / "broken.json"
    malformed.write_text("{bad json")
    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    removed: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "remove_anchored",
        lambda target: removed.append(os.fspath(target)),
    )
    registry.invalidate_cache()
    liveness.invalidate_cache()
    target = Session(
        sid="sid",
        cwd="/tmp/p",
        label="session",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=os.fspath(transcript),
    )

    result = cleanup.execute_session_removals([target])

    assert removed == []
    assert len(result.refused) == 1
    assert result.issues[0].source == "session registry"
    assert result.issues[0].path == os.fspath(malformed)
    assert transcript.exists()


def test_session_execution_refuses_real_agents_nonzero_before_removal(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(cleanup.proc, "ancestor_pids", lambda: set())
    transcript = tmp_path / "sid.jsonl"
    transcript.write_text("{}")
    completed = subprocess.CompletedProcess(
        [],
        9,
        stdout='[{"sessionId":"sid","pid":99}]',
        stderr="daemon failed",
    )
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    removed: list[str] = []
    monkeypatch.setattr(
        cleanup,
        "remove_anchored",
        lambda target: removed.append(os.fspath(target)),
    )
    registry.invalidate_cache()
    liveness.invalidate_cache()
    target = Session(
        sid="sid",
        cwd="/tmp/p",
        label="session",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=os.fspath(transcript),
    )

    result = cleanup.execute_session_removals([target])

    assert removed == []
    assert len(result.refused) == 1
    assert result.issues[0].source == "claude agents --json"
    assert "exit status 9" in result.issues[0].error
    assert transcript.exists()


def test_session_execution_allows_normal_complete_empty_protection_sources(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(cleanup.proc, "ancestor_pids", lambda: set())
    transcript = tmp_path / "sid.jsonl"
    transcript.write_text("{}")
    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    monkeypatch.setattr(liveness.subprocess, "run", lambda *a, **k: completed)
    registry.invalidate_cache()
    liveness.invalidate_cache()
    target = Session(
        sid="sid",
        cwd="/tmp/p",
        label="session",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=os.fspath(transcript),
    )

    result = cleanup.execute_session_removals([target])

    assert result.completed == ["sid"]
    assert not result.incomplete
    assert not transcript.exists()
