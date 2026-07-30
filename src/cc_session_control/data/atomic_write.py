"""Bottom-layer atomic same-directory tmp-file replacement.

Every settings/ledger writer in this package needs the same primitive: write
to a hidden tmp file beside the target, flush, fsync, close, then
``os.replace`` it into place — cleaning up the tmp file and surfacing exactly
which stage failed when something goes wrong. This module owns that pipeline
once; callers keep their own typed failure surface and map
:class:`AtomicWriteStage` onto it.

Pure stdlib IO — this module imports nothing else from this package, so it
stays at the bottom of the ``data/`` DAG.
"""

from __future__ import annotations

import os
import tempfile
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
