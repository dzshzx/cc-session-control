"""Typed cleanup results and the filesystem-removal seam.

Plan freeze records each target's lstat identity (dev/inode/file type);
execution re-lstats and refuses on any mismatch. Adversarial in-execution
races are out of scope for this single-operator local tool.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from ..models import Session

type Pathish = str | os.PathLike[str]


class RemovalStatus(StrEnum):
    """Observable outcome of one filesystem removal attempt."""

    REMOVED = "removed"
    MISSING = "missing"
    FAILED = "failed"
    REFUSED = "refused"


@dataclass(frozen=True)
class FileIdentity:
    """Filesystem identity captured without following the target itself."""

    device: int
    inode: int
    file_type: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> FileIdentity:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            file_type=stat.S_IFMT(metadata.st_mode),
        )

    def matches(self, metadata: os.stat_result) -> bool:
        return self == FileIdentity.from_stat(metadata)


@dataclass(frozen=True)
class RemovalAnchor:
    """Preview-time root and target identity for one later removal."""

    configured_root: Path
    canonical_root: Path
    root_identity: FileIdentity | None
    configured_target: Path
    relative_target: Path
    target_identity: FileIdentity | None

    @property
    def display_path(self) -> Path:
        return self.configured_target


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
    def failed(self) -> list[PathRemoval]:
        return [item for item in self.removals if item.status is RemovalStatus.FAILED]

    @property
    def incomplete(self) -> bool:
        """Whether a requested target was blocked, skipped, or failed."""
        return bool(self.failed or self.skipped or self.refused or self.issues)

    def add_removal(self, removal: PathRemoval) -> None:
        self.removals.append(removal)
        if removal.status is RemovalStatus.REFUSED:
            self.refused.append(
                CleanupNotice(str(removal.path), removal.error or "unsafe removal")
            )

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
    """Expected source problem that made a plan partial or unavailable."""

    source: str
    error: str
    path: str | None = None


@dataclass(frozen=True)
class CleanupPlan:
    """Frozen cleanup candidates shared by counts, preview, and execution."""

    empty: tuple[Session, ...] = ()
    short: tuple[Session, ...] = ()
    orphan_entries: tuple[str, ...] = ()
    zombie_pids: tuple[int, ...] = ()
    aged_entries: tuple[str, ...] = ()
    issues: tuple[CleanupIssue, ...] = ()
    session_anchors: Mapping[str, tuple[RemovalAnchor, ...]] = field(
        default_factory=dict
    )
    orphan_anchors: Mapping[str, RemovalAnchor] = field(default_factory=dict)
    zombie_anchors: Mapping[int, RemovalAnchor] = field(default_factory=dict)
    aged_anchors: Mapping[str, RemovalAnchor] = field(default_factory=dict)
    session_keyed_issue: CleanupIssue | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "empty", tuple(self.empty))
        object.__setattr__(self, "short", tuple(self.short))
        object.__setattr__(self, "orphan_entries", tuple(self.orphan_entries))
        object.__setattr__(self, "zombie_pids", tuple(self.zombie_pids))
        object.__setattr__(self, "aged_entries", tuple(self.aged_entries))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(
            self,
            "session_anchors",
            MappingProxyType(
                {sid: tuple(anchors) for sid, anchors in self.session_anchors.items()}
            ),
        )
        object.__setattr__(
            self,
            "orphan_anchors",
            MappingProxyType(dict(self.orphan_anchors)),
        )
        object.__setattr__(
            self,
            "zombie_anchors",
            MappingProxyType(dict(self.zombie_anchors)),
        )
        object.__setattr__(
            self,
            "aged_anchors",
            MappingProxyType(dict(self.aged_anchors)),
        )
        if self.session_keyed_issue is not None and (
            self.empty
            or self.short
            or self.orphan_entries
            or self.zombie_pids
            or self.session_anchors
            or self.orphan_anchors
            or self.zombie_anchors
        ):
            raise ValueError(
                "unavailable session-keyed cleanup cannot retain targets or anchors"
            )

    def counts(self) -> dict[str, int]:
        return {
            "empty": len(self.empty),
            "short": len(self.short),
            "orphan_dirs": len(self.orphan_entries),
            "zombie_procs": len(self.zombie_pids),
            "aged_entries": len(self.aged_entries),
        }

    @property
    def age_issues(self) -> tuple[CleanupIssue, ...]:
        """Expected failures that make the age inventory non-authoritative."""
        return tuple(
            issue
            for issue in self.issues
            if issue.source in {"aged_entries", "aged_removal_anchors"}
        )

    @property
    def preview_issues(self) -> tuple[CleanupIssue, ...]:
        """Every typed issue that makes some cleanup preview unavailable."""
        session_issues = (
            (self.session_keyed_issue,) if self.session_keyed_issue is not None else ()
        )
        return (*session_issues, *self.issues)


class RemovalSafetyError(OSError):
    """A requested preview target cannot be contained beneath its root."""


def _absolute(path: Pathish) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _real(path: Pathish) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _metadata(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None


def anchor_path(configured_root: Pathish, target: Pathish) -> RemovalAnchor:
    """Freeze a configured root, its canonical identity, and one child target."""
    root = _absolute(configured_root)
    configured_target = _absolute(target)
    try:
        lexical_relative = configured_target.relative_to(root)
    except ValueError as exc:
        raise RemovalSafetyError(
            f"target is outside configured root: {configured_target}"
        ) from exc
    if lexical_relative == Path("."):
        raise RemovalSafetyError("configured root itself is not a removal target")

    canonical_root = _real(root)
    root_metadata = _metadata(canonical_root)
    if root_metadata is not None and not stat.S_ISDIR(root_metadata.st_mode):
        raise RemovalSafetyError(f"configured root is not a directory: {root}")

    if root_metadata is None:
        relative_target = lexical_relative
        target_metadata = None
    else:
        canonical_parent = _real(configured_target.parent)
        try:
            relative_parent = canonical_parent.relative_to(canonical_root)
        except ValueError as exc:
            raise RemovalSafetyError(
                f"target parent escapes configured root: {configured_target}"
            ) from exc
        relative_target = relative_parent / configured_target.name
        target_metadata = _metadata(canonical_root / relative_target)

    return RemovalAnchor(
        configured_root=root,
        canonical_root=canonical_root,
        root_identity=(
            FileIdentity.from_stat(root_metadata) if root_metadata is not None else None
        ),
        configured_target=configured_target,
        relative_target=relative_target,
        target_identity=(
            FileIdentity.from_stat(target_metadata)
            if target_metadata is not None
            else None
        ),
    )


def _refused(anchor: RemovalAnchor, reason: str) -> PathRemoval:
    return PathRemoval(anchor.display_path, RemovalStatus.REFUSED, reason)


def _failed(anchor: RemovalAnchor, exc: OSError) -> PathRemoval:
    return PathRemoval(anchor.display_path, RemovalStatus.FAILED, str(exc))


def _current_paths_match(anchor: RemovalAnchor) -> str | None:
    try:
        if _real(anchor.configured_root) != anchor.canonical_root:
            return "configured root no longer resolves to the anchored root"
        anchored_parent = anchor.canonical_root / anchor.relative_target.parent
        if _real(anchor.configured_target.parent) != anchored_parent:
            return "target ancestor no longer resolves beneath the anchored root"
    except OSError as exc:
        return f"cannot verify anchored paths: {exc}"
    return None


def _revalidated_target(
    anchor: RemovalAnchor,
) -> tuple[Path, os.stat_result] | PathRemoval:
    """Re-lstat the anchored root and target and require the preview identity."""
    path_error = _current_paths_match(anchor)
    if path_error is not None:
        return _refused(anchor, path_error)

    try:
        root_metadata = os.stat(anchor.canonical_root, follow_symlinks=False)
    except FileNotFoundError:
        if anchor.root_identity is None:
            return PathRemoval(anchor.display_path, RemovalStatus.MISSING)
        return _refused(anchor, "anchored root path changed: root no longer exists")
    except NotADirectoryError as exc:
        return _refused(anchor, f"anchored root path changed: {exc}")
    except OSError as exc:
        return _failed(anchor, exc)
    if anchor.root_identity is None:
        return _refused(anchor, "anchored root appeared after preview")
    if not anchor.root_identity.matches(root_metadata):
        return _refused(anchor, "anchored root identity changed after preview")

    target = anchor.canonical_root / anchor.relative_target
    try:
        target_metadata = os.stat(target, follow_symlinks=False)
    except FileNotFoundError:
        return PathRemoval(anchor.display_path, RemovalStatus.MISSING)
    except NotADirectoryError as exc:
        return _refused(anchor, f"anchored target ancestor changed: {exc}")
    except OSError as exc:
        return _failed(anchor, exc)
    if anchor.target_identity is None:
        return _refused(anchor, "target appeared after preview")
    if not anchor.target_identity.matches(target_metadata):
        return _refused(anchor, "target identity or type changed after preview")
    return target, target_metadata


def inspect_anchored(anchor: RemovalAnchor) -> os.stat_result | PathRemoval:
    """Read target metadata only while the preview identity still matches."""
    revalidated = _revalidated_target(anchor)
    if isinstance(revalidated, PathRemoval):
        return revalidated
    _, metadata = revalidated
    return metadata


def remove_anchored(anchor: RemovalAnchor) -> PathRemoval:
    """Remove the anchored target only while its preview identity matches.

    A previewed symlink is unlinked itself (never followed); a target that
    became a symlink — or changed dev/inode/type — since preview is REFUSED.
    """
    revalidated = _revalidated_target(anchor)
    if isinstance(revalidated, PathRemoval):
        return revalidated
    target, metadata = revalidated
    try:
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(target)
        else:
            os.unlink(target)
    except FileNotFoundError as exc:
        if os.path.lexists(target):
            return _failed(anchor, exc)
        return PathRemoval(anchor.display_path, RemovalStatus.MISSING)
    except OSError as exc:
        return _failed(anchor, exc)
    return PathRemoval(anchor.display_path, RemovalStatus.REMOVED)
