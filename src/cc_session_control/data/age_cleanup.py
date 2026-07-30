"""Session-agnostic age cleanup planning and anchored execution."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ..config import cfg
from .cleanup_anchors import entry_anchors
from .removal import (
    CleanupExecution,
    CleanupIssue,
    CleanupPlan,
    PathRemoval,
    RemovalAnchor,
    RemovalStatus,
    inspect_anchored,
    remove_anchored,
)

_AGE_DIRS = ("shell_snapshots", "telemetry", "plans", "backups", "paste_cache")
_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class AgeCleanupPlan:
    """One immutable age inventory with the anchors used by later execution."""

    entries: tuple[str, ...] = ()
    anchors: Mapping[str, RemovalAnchor] = field(default_factory=dict)
    issues: tuple[CleanupIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        object.__setattr__(self, "anchors", MappingProxyType(dict(self.anchors)))
        object.__setattr__(self, "issues", tuple(self.issues))

    def to_cleanup_plan(self, base: CleanupPlan | None = None) -> CleanupPlan:
        """Attach this captured age projection without exposing field mapping."""
        plan = CleanupPlan() if base is None else base
        return replace(
            plan,
            aged_entries=self.entries,
            aged_anchors=self.anchors,
            issues=(*plan.issues, *self.issues),
        )


def _age_dir_paths() -> list[tuple[str, str]]:
    return [
        (name.replace("_", "-"), str(getattr(cfg, f"{name}_dir"))) for name in _AGE_DIRS
    ]


def _age_cutoff(now: float) -> float:
    return now - cfg.cleanup_age_days * _SECONDS_PER_DAY


def list_aged_entries(now: float | None = None) -> list[str]:
    """Return ``<dir>/<name>`` entries older than the configured age."""
    cutoff = _age_cutoff(time.time() if now is None else now)
    out: list[str] = []
    for label, path in _age_dir_paths():
        try:
            names = os.listdir(path)
        except FileNotFoundError:
            continue
        for name in names:
            full = os.path.join(path, name)
            try:
                if os.lstat(full).st_mtime < cutoff:
                    out.append(os.path.join(label, name))
            except FileNotFoundError:
                continue
    return sorted(out)


def _plan_source[PlanItems](
    source: str,
    load: Callable[[], PlanItems],
    issues: list[CleanupIssue],
    empty: PlanItems,
) -> PlanItems:
    try:
        return load()
    except OSError as exc:
        issues.append(
            CleanupIssue(
                source=source,
                error=str(exc),
                path=os.fspath(exc.filename) if exc.filename else None,
            )
        )
        return empty


def build_age_plan(now: float | None = None) -> AgeCleanupPlan:
    """Scan and anchor age targets once, retaining expected source issues."""
    plan_now = time.time() if now is None else now
    issues: list[CleanupIssue] = []
    entries: list[str] = _plan_source(
        "aged_entries",
        lambda: list_aged_entries(plan_now),
        issues,
        [],
    )
    anchors: dict[str, RemovalAnchor] = _plan_source(
        "aged_removal_anchors",
        lambda: entry_anchors(entries, dict(_age_dir_paths())),
        issues,
        {},
    )
    return AgeCleanupPlan(
        entries=tuple(entry for entry in entries if entry in anchors),
        anchors=anchors,
        issues=tuple(issues),
    )


def _is_child_name(name: str) -> bool:
    return name not in ("", ".", "..") and os.sep not in name


def _execution_anchors(
    entries: Sequence[str],
    bases: Mapping[str, str],
    result: CleanupExecution,
) -> dict[str, RemovalAnchor]:
    try:
        return entry_anchors(entries, bases)
    except OSError as exc:
        result.refuse(entries, f"cannot establish removal anchor: {exc}")
        return {}


def execute_aged_removals(
    entries: list[str],
    now: float | None = None,
    *,
    anchors: Mapping[str, RemovalAnchor] | None = None,
) -> CleanupExecution:
    """Remove anchored preview entries that are still older than the cutoff."""
    cutoff = _age_cutoff(time.time() if now is None else now)
    base_by_label = dict(_age_dir_paths())
    result = CleanupExecution()
    if anchors is None:
        anchors = _execution_anchors(entries, base_by_label, result)
    for entry in entries:
        label, _, name = entry.partition("/")
        base = base_by_label.get(label)
        if not base or not _is_child_name(name):
            result.skip(entry, "not a previewable aged-entry path")
            continue
        anchor = anchors.get(entry)
        if anchor is None:
            result.refuse([entry], "removal anchor is missing from preview")
            continue
        inspection = inspect_anchored(anchor)
        if isinstance(inspection, PathRemoval):
            result.add_removal(inspection)
            if inspection.status is RemovalStatus.MISSING:
                result.mark_missing(entry)
            continue
        if inspection.st_mtime >= cutoff:
            result.skip(entry, "entry is no longer old enough")
            continue
        removal = remove_anchored(anchor)
        result.add_removal(removal)
        if removal.status is RemovalStatus.REMOVED:
            result.complete(entry)
        elif removal.status is RemovalStatus.MISSING:
            result.mark_missing(entry)
    return result
