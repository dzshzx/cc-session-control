"""Operator curation store — the one csctl-OWNED membership evidence source.

`projects.json` (`cfg.curation_file`, XDG config home) holds the two
operator-curated directory lists the evidence-tier membership model
(ADR-0007) consults:

- ``pinned``: directories that are projects because the operator said so —
  immune to temp/dead-dir hygiene and to observed-tier decay.
- ``hidden``: directories the operator suppressed — hidden wins over every
  evidence tier until unhidden.

The two lists are mutually exclusive: pinning a directory unhides it, hiding
unpins it (the writers below enforce it, so hand-edited files are the only
way to contradict). Writers serialize read-modify-write behind an advisory
lock and atomically replace the file, preserving JSON keys csctl does not
own. Every OTHER membership source (the CLI trust stores) stays read-only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .atomic_write import (
    AdvisoryLockError,
    AdvisoryLockStage,
    AtomicWriteError,
    AtomicWriteStage,
    advisory_lock,
    atomic_replace,
)

_LIST_KEYS = ("pinned", "hidden")


class CurationState(Enum):
    """Availability of the curation store."""

    AVAILABLE = "available"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    INVALID = "invalid"


@dataclass(frozen=True)
class CurationResult:
    """A curation read whose external failure remains observable."""

    state: CurationState
    pinned: frozenset[str] = frozenset()
    hidden: frozenset[str] = frozenset()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pinned", frozenset(self.pinned))
        object.__setattr__(self, "hidden", frozenset(self.hidden))

    @property
    def available(self) -> bool:
        return self.state in {CurationState.AVAILABLE, CurationState.MISSING}


class CurationWriteState(Enum):
    """Outcome of changing one curation list."""

    UPDATED = "updated"
    UNCHANGED = "unchanged"
    FAILED = "failed"


class CurationWriteFailure(Enum):
    """External boundary that prevented a curation update."""

    CREATE_DIRECTORY = "create-directory"
    LOCK = "lock"
    READ = "read"
    MALFORMED = "malformed"
    INVALID = "invalid"
    WRITE = "write"
    FSYNC = "fsync"
    REPLACE = "replace"
    CLEANUP = "cleanup"
    UNLOCK = "unlock"


@dataclass(frozen=True)
class CurationWriteResult:
    """Typed, operator-visible result of an atomic curation write."""

    state: CurationWriteState
    path: Path
    failure: CurationWriteFailure | None = None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is not CurationWriteState.FAILED


def _validate(document: object) -> str | None:
    """The curation schema; returns the invalidity detail or None."""
    if not isinstance(document, dict):
        return "top-level JSON value is not an object"
    for key in _LIST_KEYS:
        value = document.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.startswith("/") for item in value
        ):
            return f"{key!r} is not a list of absolute paths"
    return None


def read_curation(path: Path) -> CurationResult:
    """Read the curation store without conflating absence with failure."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return CurationResult(CurationState.MISSING)
    except OSError as exc:
        return CurationResult(CurationState.UNREADABLE, detail=str(exc))
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        return CurationResult(CurationState.MALFORMED, detail=str(exc))
    invalid = _validate(document)
    if invalid is not None:
        return CurationResult(CurationState.INVALID, detail=invalid)
    return CurationResult(
        CurationState.AVAILABLE,
        # Identity is the normpath'd absolute path everywhere (ADR-0007), so
        # a hand-edited trailing slash cannot fork a directory in two.
        pinned=frozenset(os.path.normpath(item) for item in document.get("pinned", [])),
        hidden=frozenset(os.path.normpath(item) for item in document.get("hidden", [])),
    )


def _failure(
    path: Path,
    failure: CurationWriteFailure,
    detail: str,
) -> CurationWriteResult:
    return CurationWriteResult(CurationWriteState.FAILED, path, failure, detail)


def _load_for_write(path: Path) -> dict[str, Any] | CurationWriteResult:
    """Read-modify-write half: a broken store is never clobbered silently."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        return _failure(path, CurationWriteFailure.READ, str(exc))
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        return _failure(path, CurationWriteFailure.MALFORMED, str(exc))
    invalid = _validate(document)
    if invalid is not None:
        return _failure(path, CurationWriteFailure.INVALID, invalid)
    return document


_STAGE_TO_FAILURE: Mapping[AtomicWriteStage, CurationWriteFailure] = {
    AtomicWriteStage.CREATE: CurationWriteFailure.WRITE,
    AtomicWriteStage.WRITE: CurationWriteFailure.WRITE,
    AtomicWriteStage.FSYNC: CurationWriteFailure.FSYNC,
    AtomicWriteStage.REPLACE: CurationWriteFailure.REPLACE,
    AtomicWriteStage.CLEANUP: CurationWriteFailure.CLEANUP,
}


def _replace(path: Path, document: dict[str, Any]) -> CurationWriteResult:
    content = json.dumps(document, indent=2) + "\n"
    try:
        atomic_replace(path, content)
    except AtomicWriteError as exc:
        return _failure(path, _STAGE_TO_FAILURE[exc.stage], exc.detail)
    return CurationWriteResult(CurationWriteState.UPDATED, path)


def _set_membership(
    document: dict[str, Any],
    key: str,
    directory: str,
    present: bool,
) -> bool:
    """Add/remove `directory` in one list; returns whether anything changed."""
    entries: list[str] = document.get(key, [])
    if present and directory not in entries:
        document[key] = sorted([*entries, directory])
        return True
    if not present and directory in entries:
        document[key] = [item for item in entries if item != directory]
        return True
    return False


def _locked_update(
    path: Path,
    mutate: Callable[[dict[str, Any]], bool],
) -> CurationWriteResult:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failure(path, CurationWriteFailure.CREATE_DIRECTORY, str(exc))

    lock_path = path.with_name(f".{path.name}.lock")
    outcome: CurationWriteResult | None = None
    try:
        with advisory_lock(lock_path):
            document = _load_for_write(path)
            if isinstance(document, CurationWriteResult):
                outcome = document
            elif mutate(document):
                outcome = _replace(path, document)
            else:
                outcome = CurationWriteResult(CurationWriteState.UNCHANGED, path)
    except AdvisoryLockError as exc:
        detail = exc.detail
        if outcome is not None and not outcome.success:
            previous = (
                outcome.failure.value
                if outcome.failure is not None
                else outcome.state.value
            )
            detail = f"{previous}: {outcome.detail}; unlock: {detail}"
        failure = (
            CurationWriteFailure.LOCK
            if exc.stage is AdvisoryLockStage.LOCK
            else CurationWriteFailure.UNLOCK
        )
        return _failure(path, failure, detail)
    if outcome is None:
        raise RuntimeError("curation transaction produced no result")
    return outcome


def set_pinned(path: Path, directory: str, pinned: bool) -> CurationWriteResult:
    """Pin/unpin one directory; pinning also unhides it (mutual exclusion)."""

    target = os.path.normpath(directory)

    def mutate(document: dict[str, Any]) -> bool:
        changed = _set_membership(document, "pinned", target, pinned)
        if pinned:
            changed |= _set_membership(document, "hidden", target, False)
        return changed

    return _locked_update(path, mutate)


def set_hidden(path: Path, directory: str, hidden: bool) -> CurationWriteResult:
    """Hide/unhide one directory; hiding also unpins it (mutual exclusion)."""

    target = os.path.normpath(directory)

    def mutate(document: dict[str, Any]) -> bool:
        changed = _set_membership(document, "hidden", target, hidden)
        if hidden:
            changed |= _set_membership(document, "pinned", target, False)
        return changed

    return _locked_update(path, mutate)
