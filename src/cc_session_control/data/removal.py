"""Typed cleanup results and the filesystem-removal seam."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..models import Session

type Pathish = str | os.PathLike[str]


class RemovalStatus(StrEnum):
    """Observable outcome of one filesystem removal attempt."""

    REMOVED = "removed"
    MISSING = "missing"
    FAILED = "failed"


@dataclass(frozen=True)
class PathRemoval:
    """Removal outcome for one exact path."""

    path: Path
    status: RemovalStatus
    error: str | None = None


@dataclass(frozen=True)
class CleanupNotice:
    """A cleanup target that was intentionally not sent to the filesystem."""

    target: str
    reason: str


@dataclass
class CleanupExecution:
    """Aggregate result of one confirmed cleanup action."""

    removals: list[PathRemoval] = field(default_factory=list)
    skipped: list[CleanupNotice] = field(default_factory=list)
    refused: list[CleanupNotice] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    missing_targets: list[str] = field(default_factory=list)
    issues: list[CleanupIssue] = field(default_factory=list)

    @property
    def removed(self) -> list[PathRemoval]:
        return [item for item in self.removals if item.status is RemovalStatus.REMOVED]

    @property
    def missing(self) -> list[PathRemoval]:
        return [item for item in self.removals if item.status is RemovalStatus.MISSING]

    @property
    def failed(self) -> list[PathRemoval]:
        return [item for item in self.removals if item.status is RemovalStatus.FAILED]

    @property
    def incomplete(self) -> bool:
        """Whether a requested target was blocked, skipped, or failed."""
        return bool(self.failed or self.skipped or self.refused or self.issues)

    def add_removal(self, removal: PathRemoval) -> None:
        self.removals.append(removal)

    def complete(self, target: object) -> None:
        self.completed.append(str(target))

    def mark_missing(self, target: object) -> None:
        self.missing_targets.append(str(target))

    def skip(self, target: object, reason: str) -> None:
        self.skipped.append(CleanupNotice(str(target), reason))

    def refuse(self, targets: Iterable[object], reason: str) -> None:
        self.refused.extend(CleanupNotice(str(target), reason) for target in targets)

    def extend(self, other: CleanupExecution) -> None:
        self.removals.extend(other.removals)
        self.skipped.extend(other.skipped)
        self.refused.extend(other.refused)
        self.completed.extend(other.completed)
        self.missing_targets.extend(other.missing_targets)
        self.issues.extend(other.issues)


@dataclass(frozen=True)
class CleanupIssue:
    """Expected source I/O problem that made a plan partial."""

    source: str
    error: str
    path: str | None = None


@dataclass
class CleanupPlan:
    """Frozen cleanup candidates shared by counts, preview, and execution."""

    empty: list[Session] = field(default_factory=list)
    short: list[Session] = field(default_factory=list)
    orphan_entries: list[str] = field(default_factory=list)
    zombie_pids: list[int] = field(default_factory=list)
    aged_entries: list[str] = field(default_factory=list)
    issues: list[CleanupIssue] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "empty": len(self.empty),
            "short": len(self.short),
            "orphan_dirs": len(self.orphan_entries),
            "zombie_procs": len(self.zombie_pids),
            "aged_entries": len(self.aged_entries),
        }


def remove_path(path: Pathish) -> PathRemoval:
    """Remove one path without following directory symlinks."""
    target = Path(path)
    try:
        metadata = os.lstat(target)
    except FileNotFoundError:
        return PathRemoval(target, RemovalStatus.MISSING)
    except OSError as exc:
        return PathRemoval(target, RemovalStatus.FAILED, str(exc))

    try:
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(target)
        else:
            os.unlink(target)
    except FileNotFoundError:
        return PathRemoval(target, RemovalStatus.MISSING)
    except OSError as exc:
        return PathRemoval(target, RemovalStatus.FAILED, str(exc))
    return PathRemoval(target, RemovalStatus.REMOVED)
