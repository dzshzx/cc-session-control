"""Typed, atomic persistence for the bridge-environment ledger."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import IO, Literal

from ..config import cfg
from ..models import BridgeEnv, EnvRecord

_RETENTION_SECONDS = 90 * 86400
_MAX_ENTRIES = 500


class LedgerReadState(Enum):
    """Availability and integrity of one ledger read."""

    READY = "ready"
    MISSING = "missing"
    PARTIAL = "partial"
    FAILED = "failed"


class LedgerFailure(Enum):
    """External boundary that prevented a ledger operation."""

    CREATE_DIRECTORY = "create-directory"
    LOCK = "lock"
    READ = "read"
    WRITE = "write"
    FSYNC = "fsync"
    REPLACE = "replace"
    CLEANUP = "cleanup"
    UNLOCK = "unlock"


class LedgerUpdateState(Enum):
    """Outcome of one locked read-modify-write."""

    WRITTEN = "written"
    UNCHANGED = "unchanged"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class LedgerWarning:
    """One recoverable malformed JSONL row."""

    line: int
    detail: str


@dataclass(frozen=True)
class LedgerRead:
    """A ledger read that never conflates absence with failure."""

    state: LedgerReadState
    entries: Mapping[tuple[str, str], BridgeEnv] = field(default_factory=dict)
    warnings: tuple[LedgerWarning, ...] = ()
    failure: LedgerFailure | None = None
    detail: str = ""
    raw_text: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entries",
            MappingProxyType(dict(self.entries)),
        )
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def usable(self) -> bool:
        return self.state is not LedgerReadState.FAILED

    @property
    def history_complete(self) -> bool:
        """Whether this read is safe for mutation and orphan classification."""

        return self.state in (LedgerReadState.READY, LedgerReadState.MISSING)


@dataclass(frozen=True)
class LedgerUpdate:
    """Typed result of merging observations into the ledger."""

    state: LedgerUpdateState
    entries: Mapping[tuple[str, str], BridgeEnv] = field(default_factory=dict)
    read: LedgerRead | None = None
    failure: LedgerFailure | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entries",
            MappingProxyType(dict(self.entries)),
        )

    @property
    def success(self) -> bool:
        return self.state in (
            LedgerUpdateState.WRITTEN,
            LedgerUpdateState.UNCHANGED,
        )

    @property
    def warnings(self) -> tuple[LedgerWarning, ...]:
        return self.read.warnings if self.read is not None else ()

    @property
    def history_available(self) -> bool:
        return self.read is not None and self.read.history_complete


class _LedgerBoundaryError(OSError):
    """Expected persistence failure annotated with its exact boundary."""

    def __init__(self, failure: LedgerFailure, detail: str) -> None:
        super().__init__(detail)
        self.failure = failure
        self.detail = detail


class _LedgerLock:
    """Dedicated advisory lock that never suppresses a body exception."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: IO[str] | None = None

    def __enter__(self) -> None:
        try:
            self.file = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            raise _LedgerBoundaryError(LedgerFailure.LOCK, str(exc)) from exc
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            detail = str(exc)
            try:
                self.file.close()
            except OSError as close_exc:
                detail += f"; lock file close failed: {close_exc}"
            raise _LedgerBoundaryError(LedgerFailure.LOCK, detail) from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if self.file is None:
            return False
        failures: list[str] = []
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        except OSError as release_exc:
            failures.append(f"flock: {release_exc}")
        try:
            self.file.close()
        except OSError as close_exc:
            failures.append(f"close: {close_exc}")
        if failures:
            detail = "; ".join(failures)
            if exc is not None:
                exc.add_note(f"ledger lock release failed: {detail}")
                return False
            raise _LedgerBoundaryError(LedgerFailure.UNLOCK, detail)
        return False


def read() -> LedgerRead:
    """Read the ledger, preserving missing and external failure states."""

    return _read(cfg.environments_ledger)


def _read(target: Path) -> LedgerRead:
    try:
        with target.open(encoding="utf-8") as source:
            text = source.read()
    except FileNotFoundError:
        return LedgerRead(LedgerReadState.MISSING)
    except (OSError, UnicodeError) as exc:
        return LedgerRead(
            LedgerReadState.FAILED,
            failure=LedgerFailure.READ,
            detail=str(exc),
        )

    entries: dict[tuple[str, str], BridgeEnv] = {}
    warnings: list[LedgerWarning] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            document = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            warnings.append(LedgerWarning(line_number, str(exc)))
            continue
        if not isinstance(document, dict):
            warnings.append(
                LedgerWarning(line_number, "JSON value is not an object"),
            )
            continue
        prefix = document.get("prefix")
        key = document.get("key")
        if not isinstance(prefix, str) or not prefix:
            warnings.append(
                LedgerWarning(line_number, "missing or invalid prefix"),
            )
            continue
        if not isinstance(key, str) or not key:
            warnings.append(LedgerWarning(line_number, "missing or invalid key"))
            continue
        bound_sid = document.get("bound_sid")
        if bound_sid is not None and not isinstance(bound_sid, str):
            warnings.append(LedgerWarning(line_number, "invalid bound_sid"))
            continue
        first_seen = _timestamp(document.get("first_seen"))
        last_seen = _timestamp(document.get("last_seen"))
        if first_seen is None or last_seen is None:
            warnings.append(LedgerWarning(line_number, "invalid timestamp"))
            continue
        entries[(prefix, key)] = BridgeEnv(
            prefix=prefix,
            key=key,
            bound_sid=bound_sid,
            first_seen=first_seen,
            last_seen=last_seen,
        )
    state = LedgerReadState.PARTIAL if warnings else LedgerReadState.READY
    return LedgerRead(state, entries, tuple(warnings), raw_text=text)


def _timestamp(value: object) -> float | None:
    """Parse a durable timestamp; missing legacy fields retain the old zero."""

    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    return timestamp if math.isfinite(timestamp) else None


def update(
    records: Sequence[EnvRecord],
    now: float | None = None,
) -> LedgerUpdate:
    """Merge observations under one lock and atomically persist real changes."""

    target = cfg.environments_ledger
    timestamp = time.time() if now is None else now
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failed(LedgerFailure.CREATE_DIRECTORY, str(exc))

    try:
        with _LedgerLock(target.with_name(f"{target.name}.lock")):
            previous = _read(target)
            if previous.state is LedgerReadState.PARTIAL:
                return LedgerUpdate(
                    LedgerUpdateState.BLOCKED,
                    _copy_entries(previous.entries),
                    previous,
                    detail="partial ledger history is unsafe to rewrite",
                )
            if not previous.usable:
                return _failed(
                    previous.failure or LedgerFailure.READ,
                    previous.detail,
                    read_result=previous,
                )
            old_entries = _copy_entries(previous.entries)
            updated = _merge(_copy_entries(previous.entries), records, timestamp)
            updated = _compact(updated, timestamp)
            canonical = _serialize(updated)
            changed = (
                _membership(updated) != _membership(old_entries)
                or _serialize(old_entries) != previous.raw_text
            )
            if not changed:
                return LedgerUpdate(
                    LedgerUpdateState.UNCHANGED,
                    old_entries,
                    previous,
                )
            try:
                _atomic_replace(target, canonical)
            except _LedgerBoundaryError as exc:
                return _failed(
                    exc.failure,
                    exc.detail,
                    read_result=previous,
                )
            return LedgerUpdate(LedgerUpdateState.WRITTEN, updated, previous)
    except _LedgerBoundaryError as exc:
        return _failed(exc.failure, exc.detail)


def _failed(
    failure: LedgerFailure,
    detail: str,
    *,
    read_result: LedgerRead | None = None,
) -> LedgerUpdate:
    entries = (
        _copy_entries(read_result.entries)
        if read_result is not None and read_result.usable
        else {}
    )
    return LedgerUpdate(
        LedgerUpdateState.FAILED,
        entries,
        read_result,
        failure,
        detail,
    )


def _copy_entries(
    entries: Mapping[tuple[str, str], BridgeEnv],
) -> dict[tuple[str, str], BridgeEnv]:
    return {
        key: BridgeEnv(
            prefix=value.prefix,
            key=value.key,
            bound_sid=value.bound_sid,
            first_seen=value.first_seen,
            last_seen=value.last_seen,
        )
        for key, value in entries.items()
    }


def _merge(
    entries: dict[tuple[str, str], BridgeEnv],
    records: Sequence[EnvRecord],
    now: float,
) -> dict[tuple[str, str], BridgeEnv]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for record in records:
        if not record.prefix or not record.key:
            continue
        if record.bound_sid:
            grouped.setdefault((record.prefix, record.key), []).append(
                record.bound_sid,
            )
        else:
            grouped.setdefault((record.prefix, record.key), [])
    for (prefix, key), bound_sids in grouped.items():
        bound_sid = min(bound_sids) if bound_sids else None
        existing = entries.get((prefix, key))
        if existing is None:
            entries[(prefix, key)] = BridgeEnv(
                prefix=prefix,
                key=key,
                bound_sid=bound_sid,
                first_seen=now,
                last_seen=now,
            )
        else:
            entries[(prefix, key)] = replace(
                existing,
                bound_sid=bound_sid,
                last_seen=now,
            )
    return entries


def _compact(
    entries: dict[tuple[str, str], BridgeEnv],
    now: float,
) -> dict[tuple[str, str], BridgeEnv]:
    cutoff = now - _RETENTION_SECONDS
    kept = {key: value for key, value in entries.items() if value.last_seen >= cutoff}
    if len(kept) <= _MAX_ENTRIES:
        return kept
    newest = sorted(
        kept.values(),
        key=lambda entry: entry.last_seen,
        reverse=True,
    )
    return {(entry.prefix, entry.key): entry for entry in newest[:_MAX_ENTRIES]}


def _membership(
    entries: dict[tuple[str, str], BridgeEnv],
) -> set[tuple[str, str, str | None, float]]:
    return {
        (entry.prefix, entry.key, entry.bound_sid, entry.first_seen)
        for entry in entries.values()
    }


def _serialize(entries: dict[tuple[str, str], BridgeEnv]) -> str:
    lines = [
        json.dumps(
            {
                "prefix": entry.prefix,
                "key": entry.key,
                "bound_sid": entry.bound_sid,
                "first_seen": entry.first_seen,
                "last_seen": entry.last_seen,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        for entry in sorted(
            entries.values(),
            key=lambda item: (item.prefix, item.key),
        )
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _atomic_replace(path: Path, text: str) -> None:
    try:
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
    except OSError as exc:
        raise _LedgerBoundaryError(LedgerFailure.WRITE, str(exc)) from exc
    temporary_path = Path(temporary.name)

    try:
        try:
            temporary.write(text)
            temporary.flush()
        except (OSError, UnicodeError) as exc:
            raise _LedgerBoundaryError(LedgerFailure.WRITE, str(exc)) from exc
        try:
            os.fsync(temporary.fileno())
        except OSError as exc:
            raise _LedgerBoundaryError(LedgerFailure.FSYNC, str(exc)) from exc
        try:
            temporary.close()
        except OSError as exc:
            raise _LedgerBoundaryError(LedgerFailure.WRITE, str(exc)) from exc
        try:
            os.replace(temporary_path, path)
        except OSError as exc:
            raise _LedgerBoundaryError(LedgerFailure.REPLACE, str(exc)) from exc
    except _LedgerBoundaryError as outcome:
        cleanup_errors: list[str] = []
        if not temporary.closed:
            try:
                temporary.close()
            except OSError as exc:
                cleanup_errors.append(f"close: {exc}")
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_errors.append(f"unlink: {exc}")
        if cleanup_errors:
            raise _LedgerBoundaryError(
                LedgerFailure.CLEANUP,
                f"{outcome.failure.value}: {outcome.detail}; "
                + "; ".join(cleanup_errors),
            ) from outcome
        raise
