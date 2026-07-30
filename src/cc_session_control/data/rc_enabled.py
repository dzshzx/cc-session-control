"""Locked transactions for the path-keyed ``rc-enabled`` list.

The lock, legacy migration, canonicalization, mutation, and atomic replacement
are one module so every caller observes one serialized generation.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import IO, Literal, TypeVar

from .atomic_write import AtomicWriteError, AtomicWriteStage, atomic_replace

_T = TypeVar("_T")
_Mutation = Callable[[list[str]], tuple[list[str], _T]]


class EnabledListOperation(Enum):
    """Operation performed by one enabled-list transaction."""

    LIST = "list"
    ADD = "add"
    REMOVE = "remove"
    TOGGLE = "toggle"


class EnabledListState(Enum):
    """Outcome state of one enabled-list transaction."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EnabledListStage(Enum):
    """External persistence boundary that failed."""

    CREATE_DIRECTORY = "create-directory"
    LOCK = "lock"
    CREATE_FILE = "create-file"
    READ = "read"
    WRITE = "write"
    FSYNC = "fsync"
    REPLACE = "replace"
    CLEANUP = "cleanup"
    UNLOCK = "unlock"


@dataclass(frozen=True)
class EnabledListResult[Value]:
    """Immutable outcome of one serialized enabled-list operation."""

    operation: EnabledListOperation
    state: EnabledListState
    value: Value | None
    changed: bool
    committed: bool
    stage: EnabledListStage | None = None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is EnabledListState.SUCCEEDED


class _EnabledListBoundaryError(OSError):
    """Expected persistence failure annotated with its exact stage."""

    def __init__(self, stage: EnabledListStage, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


_STAGE_TO_STAGE: Mapping[AtomicWriteStage, EnabledListStage] = {
    AtomicWriteStage.CREATE: EnabledListStage.WRITE,
    AtomicWriteStage.WRITE: EnabledListStage.WRITE,
    AtomicWriteStage.FSYNC: EnabledListStage.FSYNC,
    AtomicWriteStage.REPLACE: EnabledListStage.REPLACE,
    AtomicWriteStage.CLEANUP: EnabledListStage.CLEANUP,
}


class _EnabledListLock:
    """Dedicated flock whose release and close failures stay observable."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[bytes] | None = None

    def __enter__(self) -> None:
        try:
            self._file = self._path.open("a+b")
        except (OSError, UnicodeError) as exc:
            raise _EnabledListBoundaryError(
                EnabledListStage.LOCK,
                str(exc),
            ) from exc
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX)
        except (OSError, UnicodeError) as exc:
            detail = str(exc)
            try:
                self._file.close()
            except OSError as close_exc:
                detail += f"; close: {close_exc}"
            raise _EnabledListBoundaryError(
                EnabledListStage.LOCK,
                detail,
            ) from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if self._file is None:
            return False
        failures: list[str] = []
        try:
            fcntl.flock(self._file, fcntl.LOCK_UN)
        except OSError as unlock_exc:
            failures.append(f"flock: {unlock_exc}")
        try:
            self._file.close()
        except OSError as close_exc:
            failures.append(f"close: {close_exc}")
        if not failures:
            return False
        detail = "; ".join(failures)
        if isinstance(exc, _EnabledListBoundaryError):
            raise _EnabledListBoundaryError(
                EnabledListStage.UNLOCK,
                f"{exc.stage.value}: {exc.detail}; unlock: {detail}",
            ) from exc
        if exc is not None:
            exc.add_note(f"enabled-list lock release failed: {detail}")
            return False
        raise _EnabledListBoundaryError(EnabledListStage.UNLOCK, detail)


def _canonical(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def _path_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip() and not line.strip().startswith("#")]


def migrate_lines(
    lines: list[str],
    legacy_root: Callable[[], str],
) -> tuple[list[str], bool]:
    """Canonicalize path rows once, retaining non-path rows and first-seen order."""

    migrated: list[str] = []
    seen: set[str] = set()
    root: str | None = None
    changed = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            migrated.append(raw)
            continue
        if os.path.isabs(stripped):
            path = _canonical(stripped)
        else:
            if root is None:
                root = legacy_root()
            path = _canonical(os.path.join(root, stripped))
        if path in seen:
            changed = True
            continue
        seen.add(path)
        migrated.append(path)
        changed = changed or path != raw
    return migrated, changed


class EnabledListStore:
    """Path-injected interface for serialized ``rc-enabled`` operations."""

    def __init__(
        self,
        path: Path,
        legacy_root: Callable[[], str],
    ) -> None:
        self._path = path
        self._legacy_root = legacy_root
        self._lock_path = path.with_name(f".{path.name}.lock")

    def list_result(self) -> EnabledListResult[tuple[str, ...]]:
        return self._update_result(
            EnabledListOperation.LIST,
            lambda lines: (lines, tuple(_path_lines(lines))),
        )

    def add_result(self, path: str) -> EnabledListResult[bool]:
        canonical = _canonical(path)

        def add_path(lines: list[str]) -> tuple[list[str], bool]:
            if canonical in _path_lines(lines):
                return lines, False
            return [*lines, canonical], True

        return self._update_result(EnabledListOperation.ADD, add_path)

    def remove_result(self, path: str) -> EnabledListResult[bool]:
        canonical = _canonical(path)

        def remove_path(lines: list[str]) -> tuple[list[str], bool]:
            updated = [line for line in lines if line != canonical]
            return updated, updated != lines

        return self._update_result(EnabledListOperation.REMOVE, remove_path)

    def toggle_result(self, path: str) -> EnabledListResult[bool]:
        canonical = _canonical(path)

        def toggle_path(lines: list[str]) -> tuple[list[str], bool]:
            if canonical in _path_lines(lines):
                return [line for line in lines if line != canonical], False
            return [*lines, canonical], True

        return self._update_result(EnabledListOperation.TOGGLE, toggle_path)

    def _update_result(
        self,
        operation: EnabledListOperation,
        mutate: _Mutation[_T],
    ) -> EnabledListResult[_T]:
        """The only read-modify-write seam; no operation bypasses its lock."""

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, UnicodeError) as exc:
            return self._failure(
                operation,
                EnabledListStage.CREATE_DIRECTORY,
                str(exc),
            )
        result: EnabledListResult[_T] | None = None
        try:
            with _EnabledListLock(self._lock_path):
                result = self._locked_update_result(operation, mutate)
        except _EnabledListBoundaryError as exc:
            detail = exc.detail
            if result is not None and not result.success:
                previous = (
                    result.stage.value
                    if result.stage is not None
                    else result.state.value
                )
                detail = f"{previous}: {result.detail}; unlock: {detail}"
            return self._failure(
                operation,
                exc.stage,
                detail,
                changed=result.changed if result is not None else False,
                committed=result.committed if result is not None else False,
            )
        if result is None:
            raise RuntimeError("enabled-list transaction produced no result")
        return result

    def _locked_update_result(
        self,
        operation: EnabledListOperation,
        mutate: _Mutation[_T],
    ) -> EnabledListResult[_T]:
        self._ensure_file()
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            return self._failure(operation, EnabledListStage.READ, str(exc))
        try:
            migrated, migration_changed = migrate_lines(
                lines,
                self._legacy_root,
            )
        except (OSError, UnicodeError) as exc:
            return self._failure(operation, EnabledListStage.READ, str(exc))
        updated, value = mutate(migrated)
        changed = migration_changed or updated != migrated
        if changed:
            try:
                self._replace(updated)
            except _EnabledListBoundaryError as exc:
                return self._failure(
                    operation,
                    exc.stage,
                    exc.detail,
                    changed=True,
                )
        return EnabledListResult(
            operation,
            EnabledListState.SUCCEEDED,
            value,
            changed,
            committed=changed,
        )

    @staticmethod
    def _failure(
        operation: EnabledListOperation,
        stage: EnabledListStage,
        detail: str,
        *,
        changed: bool = False,
        committed: bool = False,
    ) -> EnabledListResult[_T]:
        return EnabledListResult(
            operation,
            EnabledListState.FAILED,
            None,
            changed,
            committed,
            stage,
            detail,
        )

    def _ensure_file(self) -> None:
        try:
            with self._path.open("xb"):
                pass
        except FileExistsError:
            pass
        except (OSError, UnicodeError) as exc:
            raise _EnabledListBoundaryError(
                EnabledListStage.CREATE_FILE,
                str(exc),
            ) from exc

    def _replace(self, lines: Sequence[str]) -> None:
        content = "".join(f"{line}\n" for line in lines)
        try:
            atomic_replace(self._path, content)
        except AtomicWriteError as exc:
            raise _EnabledListBoundaryError(
                _STAGE_TO_STAGE[exc.stage],
                exc.detail,
            ) from exc
