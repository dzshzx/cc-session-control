"""Transactional rc-enabled store behavior at its public interface."""

from __future__ import annotations

import multiprocessing
from dataclasses import FrozenInstanceError
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from cc_session_control.data import atomic_write
from cc_session_control.data.rc_enabled import (
    EnabledListOperation,
    EnabledListResult,
    EnabledListStage,
    EnabledListState,
    EnabledListStore,
)


def _worker(
    store_path: str,
    legacy_root: str,
    operation: str,
    value: str,
    start: Event,
    results: Queue,
) -> None:
    """Spawn-safe worker: all store dependencies are constructed in the child."""

    store = EnabledListStore(Path(store_path), lambda: legacy_root)
    try:
        start.wait(timeout=5)
        if operation == "add":
            result = store.add_result(value).value
        elif operation == "remove":
            result = store.remove_result(value).value
        else:
            result = store.list_result().value
        results.put(("ok", result))
    except (OSError, UnicodeError) as exc:
        results.put(("error", repr(exc)))


def _run_workers(
    store_path: Path,
    legacy_root: Path,
    operations: list[tuple[str, str]],
) -> list[object]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_worker,
            args=(
                str(store_path),
                str(legacy_root),
                operation,
                value,
                start,
                results,
            ),
        )
        for operation, value in operations
    ]
    for process in processes:
        process.start()
    start.set()
    output = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("rc-enabled worker did not exit")
        assert process.exitcode == 0
    errors = [value for state, value in output if state == "error"]
    assert errors == []
    return [value for state, value in output if state == "ok"]


def _store(path: Path, legacy_root: Path | None = None) -> EnabledListStore:
    root = path.parent / "legacy" if legacy_root is None else legacy_root
    return EnabledListStore(path, lambda: str(root))


def _temporary_files(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def test_typed_list_result_is_immutable_and_reports_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rc-enabled"
    project = tmp_path / "project"
    path.write_text(f"# keep\n{project}\n", encoding="utf-8")

    result = _store(path).list_result()

    assert result.operation is EnabledListOperation.LIST
    assert result.state is EnabledListState.SUCCEEDED
    assert result.value == (str(project),)
    assert result.changed is False
    assert result.committed is False
    assert result.stage is None
    assert result.detail == ""
    with pytest.raises(FrozenInstanceError):
        result.changed = True
    with pytest.raises(AttributeError):
        result.value.append(str(tmp_path / "mutated"))


def test_typed_mutation_results_distinguish_value_changed_and_committed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rc-enabled"
    project = tmp_path / "project"
    path.write_text(f"{project}\n", encoding="utf-8")
    store = _store(path)

    duplicate = store.add_result(str(project))
    disabled = store.toggle_result(str(project))
    enabled = store.toggle_result(str(project))
    removed = store.remove_result(str(project))

    assert (
        duplicate.operation,
        duplicate.value,
        duplicate.changed,
        duplicate.committed,
    ) == (EnabledListOperation.ADD, False, False, False)
    assert (
        disabled.operation,
        disabled.value,
        disabled.changed,
        disabled.committed,
    ) == (EnabledListOperation.TOGGLE, False, True, True)
    assert (
        enabled.operation,
        enabled.value,
        enabled.changed,
        enabled.committed,
    ) == (EnabledListOperation.TOGGLE, True, True, True)
    assert (
        removed.operation,
        removed.value,
        removed.changed,
        removed.committed,
    ) == (EnabledListOperation.REMOVE, True, True, True)
    assert all(
        result.state is EnabledListState.SUCCEEDED
        for result in (duplicate, disabled, enabled, removed)
    )


def test_typed_list_result_reports_migration_commit_then_stability(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rc-enabled"
    legacy_root = tmp_path / "workspace"
    path.write_text("# keep\nlegacy\n", encoding="utf-8")
    store = _store(path, legacy_root)

    migrated = store.list_result()
    stable = store.list_result()

    assert migrated.value == (str(legacy_root / "legacy"),)
    assert migrated.changed is True
    assert migrated.committed is True
    assert stable.value == migrated.value
    assert stable.changed is False
    assert stable.committed is False


def test_typed_methods_do_not_convert_programming_errors(tmp_path: Path) -> None:
    path = tmp_path / "rc-enabled"
    path.write_text("legacy\n", encoding="utf-8")

    def broken_legacy_root() -> str:
        raise RuntimeError("legacy root invariant broken")

    store = EnabledListStore(path, broken_legacy_root)

    with pytest.raises(RuntimeError, match="legacy root invariant broken"):
        store.list_result()


def test_missing_list_is_created_and_reads_empty(tmp_path: Path) -> None:
    path = tmp_path / "config" / "rc-enabled"

    assert _store(path).list_result().value == ()
    assert path.read_bytes() == b""


def test_add_remove_toggle_are_canonical_and_preserve_layout(tmp_path: Path) -> None:
    path = tmp_path / "rc-enabled"
    one = tmp_path / "one"
    two = tmp_path / "two"
    path.write_text(f"  # before\n\n{one}/../one/\n# after\n", encoding="utf-8")
    store = _store(path)

    assert store.list_result().value == (str(one),)
    assert store.add_result(str(one / ".")).value is False
    assert store.add_result(str(two)).value is True
    assert store.remove_result(str(one / ".." / "one")).value is True
    assert store.remove_result(str(one)).value is False
    assert store.toggle_result(str(two / ".")).value is False
    assert store.toggle_result(str(one)).value is True

    assert store.list_result().value == (str(one),)
    assert path.read_text(encoding="utf-8") == f"  # before\n\n# after\n{one}\n"


def test_unchanged_operations_do_not_replace_file(tmp_path: Path) -> None:
    path = tmp_path / "rc-enabled"
    project = tmp_path / "project"
    path.write_text(f"# keep\n{project}\n", encoding="utf-8")
    store = _store(path)
    inode = path.stat().st_ino

    assert store.list_result().value == (str(project),)
    assert store.add_result(str(project)).value is False
    assert store.remove_result(str(tmp_path / "missing")).value is False

    assert path.stat().st_ino == inode
    assert path.read_text(encoding="utf-8") == f"# keep\n{project}\n"


def test_migration_is_once_and_preserves_comments_and_blank_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rc-enabled"
    legacy_root = tmp_path / "workspace"
    path.write_text("# keep\n\nold-name\n/absolute\n", encoding="utf-8")
    store = _store(path, legacy_root)

    expected = (str(legacy_root / "old-name"), "/absolute")
    assert store.list_result().value == expected
    migrated = path.read_bytes()
    inode = path.stat().st_ino
    assert migrated == (f"# keep\n\n{legacy_root / 'old-name'}\n/absolute\n".encode())

    assert store.list_result().value == expected
    assert path.stat().st_ino == inode
    assert path.read_bytes() == migrated


def test_concurrent_adds_have_no_duplicate_or_lost_update(tmp_path: Path) -> None:
    path = tmp_path / "rc-enabled"
    first = tmp_path / "first"
    second = tmp_path / "second"

    _run_workers(
        path,
        tmp_path / "workspace",
        [("add", str(first)), ("add", str(first / ".")), ("add", str(second))],
    )

    enabled = _store(path).list_result().value
    assert set(enabled) == {str(first), str(second)}
    assert len(enabled) == 2


def test_concurrent_add_and_remove_do_not_resurrect_removed_entry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rc-enabled"
    removed = tmp_path / "removed"
    added = tmp_path / "added"
    path.write_text(f"# keep\n{removed}\n", encoding="utf-8")

    _run_workers(
        path,
        tmp_path / "workspace",
        [("add", str(added)), ("remove", str(removed))],
    )

    assert _store(path).list_result().value == (str(added),)
    assert path.read_text(encoding="utf-8") == f"# keep\n{added}\n"


def test_concurrent_migration_is_serialized_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "rc-enabled"
    legacy_root = tmp_path / "workspace"
    path.write_text("# keep\n\nold\nold\n", encoding="utf-8")

    results = _run_workers(
        path,
        legacy_root,
        [("list", ""), ("list", "")],
    )

    expected = (str(legacy_root / "old"),)
    assert results == [expected, expected]
    assert path.read_text(encoding="utf-8") == f"# keep\n\n{expected[0]}\n"
    inode = path.stat().st_ino
    assert _store(path, legacy_root).list_result().value == expected
    assert path.stat().st_ino == inode


def test_typed_lock_failure_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cc_session_control.data.rc_enabled as enabled_module

    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")

    def fail_lock(_file: object, _operation: int) -> None:
        raise OSError("lock unavailable")

    monkeypatch.setattr(enabled_module.fcntl, "flock", fail_lock)

    result = _store(path).add_result(str(tmp_path / "new"))

    assert result.operation is EnabledListOperation.ADD
    assert result.state is EnabledListState.FAILED
    assert result.value is None
    assert result.changed is False
    assert result.committed is False
    assert result.stage is EnabledListStage.LOCK
    assert result.detail == "lock unavailable"
    assert path.read_bytes() == b"# original\n"
    assert _temporary_files(path) == []


def test_typed_create_directory_failure_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config" / "rc-enabled"
    original_mkdir = Path.mkdir

    def fail_parent_mkdir(target: Path, *args: object, **kwargs: object) -> None:
        if target == path.parent:
            raise UnicodeError("create directory failed")
        original_mkdir(target, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_parent_mkdir)

    result = _store(path).list_result()

    assert result.state is EnabledListState.FAILED
    assert result.stage is EnabledListStage.CREATE_DIRECTORY
    assert result.detail == "create directory failed"
    assert result.changed is False
    assert result.committed is False
    assert result.value is None


def test_typed_create_file_failure_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rc-enabled"
    original_open = Path.open

    def fail_target_create(
        target: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        if target == path and "x" in mode:
            raise OSError("create file failed")
        return original_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_target_create)

    result = _store(path).list_result()

    assert result.state is EnabledListState.FAILED
    assert result.stage is EnabledListStage.CREATE_FILE
    assert result.detail == "create file failed"
    assert result.changed is False
    assert result.committed is False
    assert result.value is None


def test_typed_create_file_and_lock_release_failures_retain_all_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cc_session_control.data.rc_enabled as enabled_module

    path = tmp_path / "rc-enabled"
    original_open = Path.open

    class CloseFailingLock:
        def close(self) -> None:
            raise OSError("lock close failed")

    def fail_boundaries(
        target: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        if mode == "a+b":
            return CloseFailingLock()
        if target == path and "x" in mode:
            raise OSError("create file failed")
        return original_open(target, mode, *args, **kwargs)

    def fail_unlock(_file: object, operation: int) -> None:
        if operation == enabled_module.fcntl.LOCK_UN:
            raise OSError("unlock failed")

    monkeypatch.setattr(Path, "open", fail_boundaries)
    monkeypatch.setattr(enabled_module.fcntl, "flock", fail_unlock)

    result = _store(path).list_result()

    assert result == EnabledListResult(
        operation=EnabledListOperation.LIST,
        state=EnabledListState.FAILED,
        value=None,
        changed=False,
        committed=False,
        stage=EnabledListStage.UNLOCK,
        detail=(
            "create-file: create file failed; "
            "unlock: flock: unlock failed; close: lock close failed"
        ),
    )


def test_typed_read_failure_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")
    original_open = Path.open

    def fail_target_read(
        target: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        if target == path and "r" in mode:
            raise UnicodeError("cannot decode enabled list")
        return original_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_target_read)

    result = _store(path).list_result()

    assert result.state is EnabledListState.FAILED
    assert result.stage is EnabledListStage.READ
    assert result.detail == "cannot decode enabled list"
    assert result.changed is False
    assert result.committed is False
    assert result.value is None
    with original_open(path, "rb") as stream:
        assert stream.read() == b"# original\n"
    assert _temporary_files(path) == []


def test_typed_write_failure_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")

    def fail_create(*_args: object, **_kwargs: object) -> object:
        raise OSError("write failed")

    monkeypatch.setattr(atomic_write.tempfile, "NamedTemporaryFile", fail_create)

    result = _store(path).add_result(str(tmp_path / "new"))

    assert result.state is EnabledListState.FAILED
    assert result.stage is EnabledListStage.WRITE
    assert result.detail == "write failed"
    assert result.changed is True
    assert result.committed is False
    assert path.read_bytes() == b"# original\n"
    assert _temporary_files(path) == []


@pytest.mark.parametrize(
    ("boundary", "stage"),
    [
        ("fsync", EnabledListStage.FSYNC),
        ("replace", EnabledListStage.REPLACE),
    ],
)
def test_typed_commit_boundary_failure_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    stage: EnabledListStage,
) -> None:
    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError(f"{boundary} failed")

    monkeypatch.setattr(atomic_write.os, boundary, fail)

    result = _store(path).add_result(str(tmp_path / "new"))

    assert result.state is EnabledListState.FAILED
    assert result.stage is stage
    assert result.detail == f"{boundary} failed"
    assert result.changed is True
    assert result.committed is False
    assert path.read_bytes() == b"# original\n"
    assert _temporary_files(path) == []


def test_typed_cleanup_failure_retains_original_failure_without_false_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")
    original_unlink = Path.unlink

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    def fail_temporary_unlink(target: Path, *args: object, **kwargs: object) -> None:
        if target.name.startswith(f".{path.name}.") and target.suffix == ".tmp":
            raise OSError("unlink failed")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(atomic_write.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)

    result = _store(path).add_result(str(tmp_path / "new"))

    assert result.state is EnabledListState.FAILED
    assert result.stage is EnabledListStage.CLEANUP
    assert result.detail == "replace: replace failed; unlink: unlink failed"
    assert result.changed is True
    assert result.committed is False
    assert path.read_bytes() == b"# original\n"
    assert len(_temporary_files(path)) == 1


def test_post_commit_unlock_failure_is_failed_but_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cc_session_control.data.rc_enabled as enabled_module

    path = tmp_path / "rc-enabled"
    new = tmp_path / "new"
    path.write_bytes(b"# original\n")
    original_flock = enabled_module.fcntl.flock

    def fail_unlock(file: object, operation: int) -> None:
        if operation == enabled_module.fcntl.LOCK_UN:
            raise OSError("unlock failed")
        original_flock(file, operation)

    monkeypatch.setattr(enabled_module.fcntl, "flock", fail_unlock)

    result = _store(path).add_result(str(new))

    assert result.operation is EnabledListOperation.ADD
    assert result.state is EnabledListState.FAILED
    assert result.stage is EnabledListStage.UNLOCK
    assert result.detail == "flock: unlock failed"
    assert result.value is None
    assert result.changed is True
    assert result.committed is True
    assert path.read_bytes() == f"# original\n{new}\n".encode()
