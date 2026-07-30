"""Bottom-layer atomic same-directory tmp-file replacement and advisory locking.

Every settings writer in this package needs the same two primitives:

- write to a hidden tmp file beside the target, flush, fsync, close, then
  ``os.replace`` it into place — cleaning up the tmp file and surfacing
  exactly which stage failed when something goes wrong (``atomic_replace``).
- serialize a read-modify-write critical section behind an ``flock`` advisory
  lock, keeping release (unlock/close) failures just as observable as the
  body's own failure (``advisory_lock``).

This module owns both pipelines once; callers keep their own typed failure
surface and map :class:`AtomicWriteStage` / :class:`AdvisoryLockStage` onto
it.

Pure stdlib IO — this module imports nothing else from this package, so it
stays at the bottom of the ``data/`` DAG.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO


class AtomicWriteStage(StrEnum):
    """External persistence boundary that failed."""

    CREATE = "create"
    WRITE = "write"
    FSYNC = "fsync"
    REPLACE = "replace"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class AtomicWriteError(Exception):
    """Expected persistence failure annotated with its exact stage."""

    stage: AtomicWriteStage
    detail: str

    def __str__(self) -> str:
        return self.detail


def atomic_replace(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``content`` via a same-directory tmp file.

    Writes a hidden ``.{path.name}.*.tmp`` sibling, flushes, fsyncs, closes,
    then ``os.replace``s it onto ``path``. On any failure the tmp file is
    best-effort cleaned up and :class:`AtomicWriteError` is raised tagged with
    the stage that failed; a cleanup failure is appended to the detail rather
    than swallowed.
    """

    try:
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
    except (OSError, UnicodeError) as exc:
        raise AtomicWriteError(AtomicWriteStage.CREATE, str(exc)) from exc
    temporary_path = Path(temporary.name)

    try:
        try:
            temporary.write(content)
            temporary.flush()
        except (OSError, UnicodeError) as exc:
            raise AtomicWriteError(AtomicWriteStage.WRITE, str(exc)) from exc
        try:
            os.fsync(temporary.fileno())
        except (OSError, UnicodeError) as exc:
            raise AtomicWriteError(AtomicWriteStage.FSYNC, str(exc)) from exc
        try:
            temporary.close()
        except (OSError, UnicodeError) as exc:
            raise AtomicWriteError(AtomicWriteStage.WRITE, str(exc)) from exc
        try:
            os.replace(temporary_path, path)
        except (OSError, UnicodeError) as exc:
            raise AtomicWriteError(AtomicWriteStage.REPLACE, str(exc)) from exc
    except AtomicWriteError as outcome:
        raise _cleanup_after_failure(temporary_path, temporary, outcome) from outcome


def _cleanup_after_failure(
    temporary_path: Path,
    temporary: IO[str],
    outcome: AtomicWriteError,
) -> AtomicWriteError:
    """Remove a failed write's tmp file, folding cleanup errors into the detail."""

    cleanup_errors: list[str] = []
    if not temporary.closed:
        try:
            temporary.close()
        except (OSError, UnicodeError) as exc:
            cleanup_errors.append(f"close: {exc}")
    try:
        temporary_path.unlink()
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        cleanup_errors.append(f"unlink: {exc}")
    if cleanup_errors:
        return AtomicWriteError(
            AtomicWriteStage.CLEANUP,
            f"{outcome.stage.value}: {outcome.detail}; " + "; ".join(cleanup_errors),
        )
    return outcome


class AdvisoryLockStage(StrEnum):
    """Boundary of an advisory-lock critical section that failed."""

    LOCK = "lock"
    UNLOCK = "unlock"


class AdvisoryLockError(OSError):
    """Expected lock-boundary failure annotated with its exact stage.

    Deliberately a plain (non-frozen-dataclass) exception: it gets thrown
    back into ``advisory_lock``'s generator by ``contextlib``, which assigns
    ``__traceback__`` on it directly — a frozen dataclass's overridden
    ``__setattr__`` would reject that assignment.
    """

    def __init__(self, stage: AdvisoryLockStage, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


@contextmanager
def advisory_lock(lock_path: Path) -> Iterator[None]:
    """Serialize a critical section behind an ``flock`` on ``lock_path``.

    Opens (creating if needed) ``lock_path`` and takes an exclusive ``flock``
    before yielding; on exit it always attempts unlock-then-close, folding any
    release failure in with whatever the body did rather than swallowing it:
    a body exception gets the release failure appended as a note and keeps
    propagating unchanged (T19 — programming errors keep propagating with
    their own type intact); a body that exits normally but fails to release
    raises a fresh :class:`AdvisoryLockError` staged ``UNLOCK`` so a
    post-commit release failure is never mistaken for silent success.
    """

    try:
        lock_file = lock_path.open("a+b")
    except OSError as exc:
        raise AdvisoryLockError(AdvisoryLockStage.LOCK, str(exc)) from exc
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    except OSError as exc:
        detail = str(exc)
        try:
            lock_file.close()
        except OSError as close_exc:
            detail += f"; close: {close_exc}"
        raise AdvisoryLockError(AdvisoryLockStage.LOCK, detail) from exc

    try:
        yield
    except BaseException as exc:
        _release(lock_file, exc)
        raise
    else:
        _release(lock_file, None)


def _release(lock_file: IO[bytes], exc: BaseException | None) -> None:
    """Unlock and close, merging release failures with the body's outcome."""

    failures: list[str] = []
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    except OSError as unlock_exc:
        failures.append(f"flock: {unlock_exc}")
    try:
        lock_file.close()
    except OSError as close_exc:
        failures.append(f"close: {close_exc}")
    if not failures:
        return
    detail = "; ".join(failures)
    if exc is not None:
        exc.add_note(f"unlock: {detail}")
        return
    raise AdvisoryLockError(AdvisoryLockStage.UNLOCK, detail)
