"""Containment tests for preview-pinned cleanup removals."""

from __future__ import annotations

import os
import time

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import cleanup, liveness
from cc_session_control.data.removal import (
    RemovalStatus,
    anchor_path,
    remove_anchored,
)
from cc_session_control.models import Session, SessionProc


def _old_enough(path, now: float) -> None:
    stamp = now - 40 * 86400
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _aged_plan(now: float):
    return cleanup.build_plan(
        [],
        liveness.LivenessSnapshot(),
        now=now,
        transcript_sids=frozenset(),
    )


def _session(path, sid: str = "session-sid") -> Session:
    return Session(
        sid=sid,
        cwd="/tmp/project",
        label="session",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=os.fspath(path),
    )


def test_public_aged_cleanup_refuses_base_replaced_by_external_symlink(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    base = cfg.shell_snapshots_dir
    target = base / "old"
    target.mkdir(parents=True)
    _old_enough(target, now)
    plan = _aged_plan(now)
    assert plan.aged_entries == ("shell-snapshots/old",)

    saved = tmp_path / "saved"
    base.rename(saved)
    outside = tmp_path / "outside"
    outside_target = outside / "old"
    outside_target.mkdir(parents=True)
    sentinel = outside_target / "keep.txt"
    sentinel.write_text("keep")
    base.symlink_to(outside, target_is_directory=True)

    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert [item.status for item in result.removals] == [RemovalStatus.REFUSED]
    assert "root" in result.refused[0].reason
    assert sentinel.read_text() == "keep"
    assert (saved / "old").is_dir()


def test_public_aged_cleanup_unlinks_final_symlink_without_following_it(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep")
    link = cfg.shell_snapshots_dir / "old-link"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    _old_enough(link, now)
    plan = _aged_plan(now)

    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert [item.status for item in result.removals] == [RemovalStatus.REMOVED]
    assert not os.path.lexists(link)
    assert sentinel.read_text() == "keep"


def test_configured_base_symlink_is_legal_but_retarget_is_refused(
    tmp_path,
    monkeypatch,
):
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    monkeypatch.setattr(cfg, "claude_home", claude_home)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    first = tmp_path / "first"
    first.mkdir()
    target = first / "old"
    target.write_text("old")
    _old_enough(target, now)
    base = cfg.shell_snapshots_dir
    base.symlink_to(first, target_is_directory=True)
    plan = _aged_plan(now)

    second = tmp_path / "second"
    second.mkdir()
    outside = second / "old"
    outside.write_text("keep")
    base.unlink()
    base.symlink_to(second, target_is_directory=True)
    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert result.refused
    assert outside.read_text() == "keep"
    assert target.read_text() == "old"


def test_unchanged_configured_base_symlink_removes_from_anchored_real_root(
    tmp_path,
    monkeypatch,
):
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    monkeypatch.setattr(cfg, "claude_home", claude_home)
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    real = tmp_path / "real"
    real.mkdir()
    target = real / "old"
    target.write_text("old")
    keep = real / "keep"
    keep.write_text("keep")
    _old_enough(target, now)
    cfg.shell_snapshots_dir.symlink_to(real, target_is_directory=True)
    plan = _aged_plan(now)

    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert [item.status for item in result.removals] == [RemovalStatus.REMOVED]
    assert not target.exists()
    assert keep.read_text() == "keep"


def test_canonical_ancestor_replaced_by_symlink_is_refused(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "tree" / "claude")
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    target = cfg.shell_snapshots_dir / "old"
    target.mkdir(parents=True)
    _old_enough(target, now)
    plan = _aged_plan(now)

    tree = tmp_path / "tree"
    saved = tmp_path / "saved-tree"
    tree.rename(saved)
    outside = tmp_path / "outside"
    outside_target = outside / "claude" / "shell-snapshots" / "old"
    outside_target.mkdir(parents=True)
    sentinel = outside_target / "keep.txt"
    sentinel.write_text("keep")
    tree.symlink_to(outside, target_is_directory=True)

    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert result.refused
    assert sentinel.read_text() == "keep"
    assert (saved / "claude" / "shell-snapshots" / "old").exists()


def test_root_inode_replacement_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    base = cfg.shell_snapshots_dir
    target = base / "old"
    target.mkdir(parents=True)
    _old_enough(target, now)
    plan = _aged_plan(now)

    saved = tmp_path / "saved-root"
    base.rename(saved)
    replacement = base / "old"
    replacement.mkdir(parents=True)
    sentinel = replacement / "keep.txt"
    sentinel.write_text("keep")

    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert result.refused
    assert "identity" in result.refused[0].reason
    assert sentinel.read_text() == "keep"
    assert (saved / "old").exists()


def test_target_inode_and_type_replacement_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    target = cfg.shell_snapshots_dir / "old"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    _old_enough(target, now)
    plan = _aged_plan(now)

    target.unlink()
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep")
    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert result.refused
    assert "identity or type" in result.refused[0].reason
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("is_directory", [False, True], ids=["file", "directory"])
def test_removal_refuses_same_name_replacement_after_preview(
    tmp_path,
    is_directory: bool,
):
    target = tmp_path / "target"
    saved = tmp_path / "anchored-target"
    if is_directory:
        target.mkdir()
        (target / "anchored.txt").write_text("anchored")
    else:
        target.write_text("anchored")
    anchor = anchor_path(tmp_path, target)

    target.rename(saved)
    if is_directory:
        target.mkdir()
        (target / "replacement.txt").write_text("replacement")
    else:
        target.write_text("replacement")

    result = remove_anchored(anchor)

    assert result.status is RemovalStatus.REFUSED
    assert "identity" in (result.error or "")
    if is_directory:
        assert (target / "replacement.txt").read_text() == "replacement"
        assert (saved / "anchored.txt").read_text() == "anchored"
    else:
        assert target.read_text() == "replacement"
        assert saved.read_text() == "anchored"


def test_previewed_target_that_disappears_is_idempotently_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    now = time.time()
    target = cfg.shell_snapshots_dir / "old"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    _old_enough(target, now)
    plan = _aged_plan(now)
    target.unlink()

    result = cleanup.execute_aged_removals(
        plan.aged_entries,
        now=now,
        anchors=plan.aged_anchors,
    )

    assert [item.status for item in result.removals] == [RemovalStatus.MISSING]
    assert result.completed == []
    assert result.missing_targets == ["shell-snapshots/old"]


def test_cleanup_plan_pins_every_cleanup_category(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(cfg, "cleanup_age_days", 14)
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    now = time.time()
    transcript = cfg.projects_root / "project" / "session-sid.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}")
    orphan = cfg.session_env_dir / "orphan"
    orphan.mkdir(parents=True)
    zombie = cfg.sessions_dir / "77.json"
    zombie.parent.mkdir(parents=True)
    zombie.write_text("{}")
    aged = cfg.shell_snapshots_dir / "old"
    aged.mkdir(parents=True)
    _old_enough(aged, now)

    plan = cleanup.build_plan(
        [_session(transcript)],
        liveness.LivenessSnapshot(
            session_procs=(SessionProc(pid=77, sid="dead", proc_alive=False),),
        ),
        now=now,
        transcript_sids=frozenset(),
    )

    assert set(plan.session_anchors) == {"session-sid"}
    assert set(plan.orphan_anchors) == {"session-env/orphan"}
    assert set(plan.zombie_anchors) == {77}
    assert set(plan.aged_anchors) == {"shell-snapshots/old"}


def test_orphan_execution_refuses_replaced_base_and_preserves_external(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    base = cfg.session_env_dir
    (base / "orphan").mkdir(parents=True)
    plan = cleanup.build_plan(
        [],
        liveness.LivenessSnapshot(),
        transcript_sids=frozenset(),
    )

    saved = tmp_path / "saved-orphans"
    base.rename(saved)
    outside = tmp_path / "outside-orphans"
    outside_target = outside / "orphan"
    outside_target.mkdir(parents=True)
    sentinel = outside_target / "keep.txt"
    sentinel.write_text("keep")
    base.symlink_to(outside, target_is_directory=True)
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
    result = cleanup.execute_orphan_removals(
        plan.orphan_entries,
        anchors=plan.orphan_anchors,
    )

    assert result.refused
    assert sentinel.read_text() == "keep"
    assert (saved / "orphan").exists()


def test_zombie_execution_refuses_root_inode_replacement(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    proc = SessionProc(pid=77, sid="dead", proc_alive=False)
    target = cfg.sessions_dir / "77.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}")
    plan = cleanup.build_plan(
        [],
        liveness.LivenessSnapshot(session_procs=(proc,)),
        transcript_sids=frozenset(),
    )

    saved = tmp_path / "saved-sessions"
    cfg.sessions_dir.rename(saved)
    replacement = cfg.sessions_dir / "77.json"
    replacement.parent.mkdir(parents=True)
    replacement.write_text("keep")
    monkeypatch.setattr(
        cleanup,
        "fresh_liveness_inputs",
        lambda: liveness.LivenessSnapshot(session_procs=(proc,)),
    )
    result = cleanup.execute_zombie_removals(
        plan.zombie_pids,
        anchors=plan.zombie_anchors,
    )

    assert result.refused
    assert replacement.read_text() == "keep"
    assert (saved / "77.json").exists()


def test_remove_session_pins_before_fresh_liveness_and_refuses_parent_swap(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    monkeypatch.setattr(
        cleanup.proc,
        "probe_current_ancestors",
        lambda: cleanup.proc.AncestorProbe(frozenset({999})),
    )
    transcript = cfg.projects_root / "project" / "session-sid.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}")
    target = _session(transcript)
    saved = tmp_path / "saved-project"
    outside = tmp_path / "outside-project"
    sentinel = outside / transcript.name

    def fresh_after_swap():
        transcript.parent.rename(saved)
        outside.mkdir()
        sentinel.write_text("keep")
        transcript.parent.symlink_to(outside, target_is_directory=True)
        return liveness.LivenessSnapshot()

    monkeypatch.setattr(
        cleanup.liveness,
        "liveness_inputs",
        fresh_after_swap,
    )
    result = cleanup.remove_session(target)

    assert result.refused
    assert sentinel.read_text() == "keep"
    assert (saved / transcript.name).exists()
