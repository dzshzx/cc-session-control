"""Public-boundary tests for the bridge-environment ledger store."""

import json
from pathlib import Path

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import atomic_write
from cc_session_control.data import environment_ledger as ledger
from cc_session_control.models import EnvRecord


def _use_ledger_dir(
    tmp_path: Path,
    monkeypatch,
) -> Path:
    monkeypatch.setattr(cfg, "config_dir", tmp_path)
    return tmp_path / "environments.jsonl"


def test_read_distinguishes_missing_ledger_from_unreadable_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)

    missing = ledger.read()

    assert missing.state is ledger.LedgerReadState.MISSING
    assert missing.entries == {}
    assert missing.usable

    path.write_text("history", encoding="utf-8")
    original_open = Path.open

    def deny_ledger(target: Path, *args, **kwargs):
        if target == path:
            raise PermissionError("ledger denied")
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_ledger)

    failed = ledger.read()

    assert failed.state is ledger.LedgerReadState.FAILED
    assert failed.failure is ledger.LedgerFailure.READ
    assert "ledger denied" in failed.detail
    assert not failed.usable


def test_read_salvages_valid_rows_and_reports_bad_line_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    valid = {
        "prefix": "cse",
        "key": "kept",
        "bound_sid": "sid-a",
        "first_seen": 10.0,
        "last_seen": 20.0,
    }
    path.write_text(
        "{broken\n" + json.dumps(valid) + "\n",
        encoding="utf-8",
    )

    result = ledger.read()

    assert result.state is ledger.LedgerReadState.PARTIAL
    assert [warning.line for warning in result.warnings] == [1]
    assert "Expecting property name" in result.warnings[0].detail
    kept = result.entries[("cse", "kept")]
    assert (kept.bound_sid, kept.first_seen, kept.last_seen) == (
        "sid-a",
        10.0,
        20.0,
    )


def test_read_invalid_encoding_is_typed_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    path.write_bytes(b"\xff\n")

    result = ledger.read()

    assert result.state is ledger.LedgerReadState.FAILED
    assert result.failure is ledger.LedgerFailure.READ
    assert not result.usable


def test_update_reports_written_then_unchanged_without_rewriting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    records = [EnvRecord("cse", "kept", "sid-a")]

    written = ledger.update(records, now=10.0)
    original = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns
    unchanged = ledger.update(records, now=20.0)

    assert written.state is ledger.LedgerUpdateState.WRITTEN
    assert unchanged.state is ledger.LedgerUpdateState.UNCHANGED
    assert unchanged.success
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_mtime


def test_update_partial_history_is_blocked_and_preserves_original_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    original = (
        b"{broken\n"
        b'{"prefix":"cse","key":"history","bound_sid":"sid-old",'
        b'"first_seen":1.0,"last_seen":2.0}\n'
    )
    path.write_bytes(original)

    result = ledger.update([EnvRecord("session", "new", "sid-new")], now=30.0)

    assert path.read_bytes() == original
    assert result.state is ledger.LedgerUpdateState.BLOCKED
    assert not result.success
    assert result.read is not None
    assert result.read.state is ledger.LedgerReadState.PARTIAL
    assert result.warnings[0].line == 1
    assert ("cse", "history") in result.entries
    assert not result.history_available
    assert result.failure is None
    assert "partial" in result.detail


def test_update_read_failure_never_replaces_existing_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    original = b'{"prefix":"cse","key":"history"}\n'
    path.write_bytes(original)
    original_open = Path.open
    replacements: list[tuple[object, object]] = []

    def fail_ledger_read(target: Path, *args, **kwargs):
        if target == path:
            raise OSError("injected read failure")
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_ledger_read)
    monkeypatch.setattr(
        atomic_write.os,
        "replace",
        lambda source, target: replacements.append((source, target)),
    )

    result = ledger.update([EnvRecord("session", "new", "sid-new")], now=30.0)

    assert result.state is ledger.LedgerUpdateState.FAILED
    assert result.failure is ledger.LedgerFailure.READ
    assert "injected read failure" in result.detail
    assert replacements == []
    with original_open(path, "rb") as source:
        assert source.read() == original


def test_update_lock_failure_is_typed_and_preserves_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    original = b'{"prefix":"cse","key":"history"}\n'
    path.write_bytes(original)

    def deny_lock(file_descriptor: int, operation: int) -> None:
        if operation == ledger.fcntl.LOCK_EX:
            raise OSError("lock unavailable")

    monkeypatch.setattr(ledger.fcntl, "flock", deny_lock)

    result = ledger.update([EnvRecord("session", "new")], now=30.0)

    assert result.state is ledger.LedgerUpdateState.FAILED
    assert result.failure is ledger.LedgerFailure.LOCK
    assert "lock unavailable" in result.detail
    assert path.read_bytes() == original


def test_update_does_not_swallow_programming_error_and_releases_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _use_ledger_dir(tmp_path, monkeypatch)

    with pytest.raises(AttributeError):
        ledger.update([object()], now=30.0)  # type: ignore[list-item]

    retry = ledger.update([EnvRecord("session", "new")], now=30.0)
    assert retry.state is ledger.LedgerUpdateState.WRITTEN


def test_update_lock_sequence_wraps_successful_atomic_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    operations: list[int] = []
    original_flock = ledger.fcntl.flock

    def record_lock(file_descriptor: int, operation: int) -> None:
        operations.append(operation)
        original_flock(file_descriptor, operation)

    monkeypatch.setattr(ledger.fcntl, "flock", record_lock)

    result = ledger.update([EnvRecord("session", "new")], now=30.0)

    assert result.state is ledger.LedgerUpdateState.WRITTEN
    assert path.exists()
    assert operations == [ledger.fcntl.LOCK_EX, ledger.fcntl.LOCK_UN]


def test_update_lock_release_failure_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    original_flock = ledger.fcntl.flock

    def fail_release(file_descriptor: int, operation: int) -> None:
        if operation == ledger.fcntl.LOCK_UN:
            raise OSError("unlock unavailable")
        original_flock(file_descriptor, operation)

    monkeypatch.setattr(ledger.fcntl, "flock", fail_release)

    result = ledger.update([EnvRecord("session", "new")], now=30.0)

    assert result.state is ledger.LedgerUpdateState.FAILED
    assert result.failure is ledger.LedgerFailure.UNLOCK
    assert "unlock unavailable" in result.detail
    assert b'"key": "new"' in path.read_bytes()


def test_update_fsync_failure_preserves_original_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    original = b'{"prefix":"cse","key":"history"}\n'
    path.write_bytes(original)
    monkeypatch.setattr(
        atomic_write.os,
        "fsync",
        lambda file_descriptor: (_ for _ in ()).throw(
            OSError("fsync unavailable"),
        ),
    )

    result = ledger.update([EnvRecord("session", "new")], now=30.0)

    assert result.state is ledger.LedgerUpdateState.FAILED
    assert result.failure is ledger.LedgerFailure.FSYNC
    assert "fsync unavailable" in result.detail
    assert result.history_available
    assert ("cse", "history") in result.entries
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".environments.jsonl.*.tmp")) == []


def test_update_temporary_creation_failure_is_typed_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    original = b'{"prefix":"cse","key":"history"}\n'
    path.write_bytes(original)
    monkeypatch.setattr(
        atomic_write.tempfile,
        "NamedTemporaryFile",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("temporary unavailable"),
        ),
    )

    result = ledger.update([EnvRecord("session", "new")], now=30.0)

    assert result.state is ledger.LedgerUpdateState.FAILED
    assert result.failure is ledger.LedgerFailure.WRITE
    assert "temporary unavailable" in result.detail
    assert path.read_bytes() == original


def test_update_replace_failure_preserves_original_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    original = b'{"prefix":"cse","key":"history"}\n'
    path.write_bytes(original)
    monkeypatch.setattr(
        atomic_write.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(
            OSError("replace unavailable"),
        ),
    )

    result = ledger.update([EnvRecord("session", "new")], now=30.0)

    assert result.state is ledger.LedgerUpdateState.FAILED
    assert result.failure is ledger.LedgerFailure.REPLACE
    assert "replace unavailable" in result.detail
    assert result.history_available
    assert ("cse", "history") in result.entries
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".environments.jsonl.*.tmp")) == []


def test_updates_use_distinct_same_directory_temporary_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _use_ledger_dir(tmp_path, monkeypatch)
    temporary_paths: list[Path] = []
    original_factory = atomic_write.tempfile.NamedTemporaryFile

    def record_temporary(*args, **kwargs):
        temporary = original_factory(*args, **kwargs)
        temporary_paths.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(atomic_write.tempfile, "NamedTemporaryFile", record_temporary)

    first = ledger.update([EnvRecord("session", "one")], now=10.0)
    second = ledger.update(
        [EnvRecord("session", "one"), EnvRecord("session", "two")],
        now=20.0,
    )

    assert (first.state, second.state) == (
        ledger.LedgerUpdateState.WRITTEN,
        ledger.LedgerUpdateState.WRITTEN,
    )
    assert len(set(temporary_paths)) == 2
    assert {temporary.parent for temporary in temporary_paths} == {path.parent}
    assert all(not temporary.exists() for temporary in temporary_paths)
