"""Direct coverage for the shared atomic tmp-file replacement primitive."""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_session_control.data import atomic_write
from cc_session_control.data.atomic_write import (
    AdvisoryLockError,
    AdvisoryLockStage,
    AtomicWriteError,
    AtomicWriteStage,
    advisory_lock,
    atomic_replace,
)


def test_atomic_replace_writes_content_via_a_hidden_same_directory_tmp_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target.txt"
    path.write_text("old")

    atomic_replace(path, "new")

    assert path.read_text() == "new"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []


def test_atomic_replace_create_failure_leaves_target_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "target.txt"
    path.write_text("old")

    def fail_create(*_args: object, **_kwargs: object) -> object:
        raise OSError("create denied")

    monkeypatch.setattr(atomic_write.tempfile, "NamedTemporaryFile", fail_create)

    with pytest.raises(AtomicWriteError) as excinfo:
        atomic_replace(path, "new")

    assert excinfo.value.stage is AtomicWriteStage.CREATE
    assert "create denied" in excinfo.value.detail
    assert path.read_text() == "old"


def test_atomic_replace_write_failure_cleans_up_tmp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "target.txt"
    path.write_text("old")
    original_factory = atomic_write.tempfile.NamedTemporaryFile

    def fail_write_factory(*args: object, **kwargs: object) -> object:
        temporary = original_factory(*args, **kwargs)
        temporary.write = lambda *_a, **_k: (_ for _ in ()).throw(
            OSError("write denied"),
        )
        return temporary

    monkeypatch.setattr(atomic_write.tempfile, "NamedTemporaryFile", fail_write_factory)

    with pytest.raises(AtomicWriteError) as excinfo:
        atomic_replace(path, "new")

    assert excinfo.value.stage is AtomicWriteStage.WRITE
    assert "write denied" in excinfo.value.detail
    assert path.read_text() == "old"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []


def test_atomic_replace_fsync_failure_cleans_up_tmp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "target.txt"
    path.write_text("old")
    monkeypatch.setattr(
        atomic_write.os,
        "fsync",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("fsync denied")),
    )

    with pytest.raises(AtomicWriteError) as excinfo:
        atomic_replace(path, "new")

    assert excinfo.value.stage is AtomicWriteStage.FSYNC
    assert "fsync denied" in excinfo.value.detail
    assert path.read_text() == "old"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []


def test_atomic_replace_replace_failure_cleans_up_tmp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "target.txt"
    path.write_text("old")
    monkeypatch.setattr(
        atomic_write.os,
        "replace",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("replace denied")),
    )

    with pytest.raises(AtomicWriteError) as excinfo:
        atomic_replace(path, "new")

    assert excinfo.value.stage is AtomicWriteStage.REPLACE
    assert "replace denied" in excinfo.value.detail
    assert path.read_text() == "old"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []


def test_atomic_replace_cleanup_failure_folds_into_detail_without_swallowing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "target.txt"
    path.write_text("old")
    original_unlink = Path.unlink

    def fail_unlink(target: Path, *args: object, **kwargs: object) -> None:
        if target.suffix == ".tmp":
            raise OSError("unlink denied")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(
        atomic_write.os,
        "replace",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("replace denied")),
    )
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(AtomicWriteError) as excinfo:
        atomic_replace(path, "new")

    assert excinfo.value.stage is AtomicWriteStage.CLEANUP
    assert excinfo.value.detail == "replace: replace denied; unlink: unlink denied"
    assert path.read_text() == "old"


def test_advisory_lock_enter_failure_raises_typed_lock_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "target.lock"

    def fail_lock(_file: object, _operation: int) -> None:
        raise OSError("lock unavailable")

    monkeypatch.setattr(atomic_write.fcntl, "flock", fail_lock)

    with pytest.raises(AdvisoryLockError) as excinfo:
        with advisory_lock(lock_path):
            raise AssertionError("body must not run when the lock is unavailable")

    assert excinfo.value.stage is AdvisoryLockStage.LOCK
    assert excinfo.value.detail == "lock unavailable"


def test_advisory_lock_body_success_and_unlock_failure_raises_fresh_unlock_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "target.lock"
    original_flock = atomic_write.fcntl.flock

    def fail_unlock(file: object, operation: int) -> None:
        if operation == atomic_write.fcntl.LOCK_UN:
            raise OSError("unlock unavailable")
        original_flock(file, operation)

    monkeypatch.setattr(atomic_write.fcntl, "flock", fail_unlock)

    body_ran = False
    with pytest.raises(AdvisoryLockError) as excinfo:
        with advisory_lock(lock_path):
            body_ran = True

    assert body_ran is True
    assert excinfo.value.stage is AdvisoryLockStage.UNLOCK
    assert excinfo.value.detail == "flock: unlock unavailable"


def test_advisory_lock_body_failure_and_unlock_failure_merges_as_a_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body exception keeps its own type; the release failure is folded in
    as a note (PEP 678) rather than replacing it — this is what lets each
    domain's own boundary-error type keep propagating uncaught release
    diagnostics attached (T19), instead of losing the original failure.
    """
    lock_path = tmp_path / "target.lock"

    def fail_unlock(_file: object, operation: int) -> None:
        if operation == atomic_write.fcntl.LOCK_UN:
            raise OSError("unlock unavailable")

    monkeypatch.setattr(atomic_write.fcntl, "flock", fail_unlock)

    with pytest.raises(ValueError) as excinfo:
        with advisory_lock(lock_path):
            raise ValueError("body failed")

    assert str(excinfo.value) == "body failed"
    assert excinfo.value.__notes__ == ["unlock: flock: unlock unavailable"]

    # The lock file was still released (close succeeded) despite the failed
    # unlock, so a subsequent acquisition does not deadlock.
    monkeypatch.undo()
    with advisory_lock(lock_path):
        pass
