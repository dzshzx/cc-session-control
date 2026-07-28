"""Transactional rc-enabled store behavior at its public interface."""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from cc_session_control.data.rc_enabled import EnabledListStore


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
            result = store.add(value)
        elif operation == "remove":
            result = store.remove(value)
        else:
            result = store.list()
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


def test_missing_list_is_created_and_reads_empty(tmp_path: Path) -> None:
    path = tmp_path / "config" / "rc-enabled"

    assert _store(path).list() == []
    assert path.read_bytes() == b""


def test_add_remove_toggle_are_canonical_and_preserve_layout(tmp_path: Path) -> None:
    path = tmp_path / "rc-enabled"
    one = tmp_path / "one"
    two = tmp_path / "two"
    path.write_text(f"  # before\n\n{one}/../one/\n# after\n", encoding="utf-8")
    store = _store(path)

    assert store.list() == [str(one)]
    assert store.contains(str(one / ".")) is True
    assert store.add(str(one / ".")) is False
    assert store.add(str(two)) is True
    assert store.remove(str(one / ".." / "one")) is True
    assert store.remove(str(one)) is False
    assert store.toggle(str(two / ".")) is False
    assert store.toggle(str(one)) is True

    assert store.list() == [str(one)]
    assert path.read_text(encoding="utf-8") == f"  # before\n\n# after\n{one}\n"


def test_unchanged_operations_do_not_replace_file(tmp_path: Path) -> None:
    path = tmp_path / "rc-enabled"
    project = tmp_path / "project"
    path.write_text(f"# keep\n{project}\n", encoding="utf-8")
    store = _store(path)
    inode = path.stat().st_ino

    assert store.list() == [str(project)]
    assert store.add(str(project)) is False
    assert store.remove(str(tmp_path / "missing")) is False

    assert path.stat().st_ino == inode
    assert path.read_text(encoding="utf-8") == f"# keep\n{project}\n"


def test_migration_is_once_and_preserves_comments_and_blank_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rc-enabled"
    legacy_root = tmp_path / "workspace"
    path.write_text("# keep\n\nold-name\n/absolute\n", encoding="utf-8")
    store = _store(path, legacy_root)

    assert store.list() == [str(legacy_root / "old-name"), "/absolute"]
    migrated = path.read_bytes()
    inode = path.stat().st_ino
    assert migrated == (
        f"# keep\n\n{legacy_root / 'old-name'}\n/absolute\n".encode()
    )

    assert store.list() == [str(legacy_root / "old-name"), "/absolute"]
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

    enabled = _store(path).list()
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

    assert _store(path).list() == [str(added)]
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

    expected = [str(legacy_root / "old")]
    assert results == [expected, expected]
    assert path.read_text(encoding="utf-8") == f"# keep\n\n{expected[0]}\n"
    inode = path.stat().st_ino
    assert _store(path, legacy_root).list() == expected
    assert path.stat().st_ino == inode


def _assert_failed_update_preserves_original(
    path: Path,
    action: Callable[[], object],
) -> None:
    original = path.read_bytes()
    with pytest.raises(OSError):
        action()
    assert path.read_bytes() == original
    assert _temporary_files(path) == []


def test_lock_failure_does_not_fall_back_to_unlocked_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cc_session_control.data.rc_enabled as enabled_module

    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")

    def fail_lock(_file: object, _operation: int) -> None:
        raise OSError("lock unavailable")

    monkeypatch.setattr(enabled_module.fcntl, "flock", fail_lock)
    _assert_failed_update_preserves_original(
        path,
        lambda: _store(path).add(str(tmp_path / "new")),
    )


def test_read_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")
    original = path.read_bytes()
    original_open = Path.open

    def fail_target_read(
        target: Path, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        if target == path and "r" in mode:
            raise OSError("read failed")
        return original_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_target_read)
    with pytest.raises(OSError):
        _store(path).list()
    with original_open(path, "rb") as stream:
        assert stream.read() == original
    assert _temporary_files(path) == []


def test_temp_write_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cc_session_control.data.rc_enabled as enabled_module

    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")

    def fail_fdopen(*_args: object, **_kwargs: object) -> object:
        raise OSError("write failed")

    monkeypatch.setattr(enabled_module.os, "fdopen", fail_fdopen)
    _assert_failed_update_preserves_original(
        path,
        lambda: _store(path).add(str(tmp_path / "new")),
    )


@pytest.mark.parametrize("boundary", ["fsync", "replace"])
def test_commit_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import cc_session_control.data.rc_enabled as enabled_module

    path = tmp_path / "rc-enabled"
    path.write_bytes(b"# original\n")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError(f"{boundary} failed")

    monkeypatch.setattr(enabled_module.os, boundary, fail)
    _assert_failed_update_preserves_original(
        path,
        lambda: _store(path).add(str(tmp_path / "new")),
    )
