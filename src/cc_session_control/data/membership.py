"""Evidence-tier project membership (ADR-0007).

A projects-tab row is no longer "a Claude-trusted directory" but an absolute
path carrying a provenance evidence set:

- **Pinned** — operator-curated via `data.curation`; immune to hygiene and
  decay.
- **Trusted** — a provider's trust store covers the directory: Claude
  effective trust (ancestor inheritance via `models.effective_trust_decision`),
  codex/kimi exact-match records (`providers.scan_trusted_dirs`).
- **Observed** — any provider has session activity (cwd) in the directory;
  an observed-only directory decays out of the tab after
  `OBSERVED_DECAY_DAYS` without new activity. Decay only affects this tab —
  the Sessions tab remains the exhaustive activity surface.

Membership = tier union − hygiene (temp roots, missing directories), where
pinned entries are exempt from hygiene, and hidden entries are returned
flagged (never filtered away here) so the view's show-hidden toggle can still
offer the unhide verb.

The load-bearing invariant: trust inheritance only ever QUALIFIES a recorded
candidate, it never GENERATES one — candidates come from explicit records
(trust-store keys, pins) and observed activity only, so a trusted `/` cannot
flood the tab.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from ..config import cfg
from ..models import (
    InventoryIssue,
    Project,
    Session,
    TrustDecision,
    effective_trust_decision,
)
from . import providers
from .curation import read_curation
from .project_settings import read_project_settings

#: Days without any provider activity after which an observed-only directory
#: leaves the projects tab (a constant, deliberately not configurable).
OBSERVED_DECAY_DAYS = 30
_SECONDS_PER_DAY = 86400

# Temp roots are working space, not projects; this affects membership, not trust.
_TEMP_ROOTS = frozenset(
    os.path.normpath(p) for p in (tempfile.gettempdir(), "/tmp", "/var/tmp")
)


def _is_temp_path(path: str) -> bool:
    """PURE: is `path` a platform temp root, or beneath one?

    Same segment-boundary matching as `models.effective_trust_decision`
    (normpath only; `/tmpfoo` is not under `/tmp`).
    """
    target = os.path.normpath(path)
    for root in _TEMP_ROOTS:
        if target == root or target.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _latest_activity(sessions: Iterable[Session]) -> dict[str, float]:
    """Newest session mtime per normpath'd cwd — shared by the observed
    tier's decay check and the projects-tab activity ordering."""
    latest: dict[str, float] = {}
    for session in sessions:
        if session.cwd:
            key = os.path.normpath(session.cwd)
            latest[key] = max(session.mtime, latest.get(key, 0.0))
    return latest


@dataclass(frozen=True)
class MembershipEntry:
    """One candidate directory and its full provenance evidence set.

    The projection intermediate between raw evidence and the view-facing
    :class:`Project`: it carries `last_activity` (needed by the decay check
    and the activity ordering), which `Project` deliberately drops.
    """

    directory: str  # normpath'd absolute path — THE identity key
    pinned: bool = False
    hidden: bool = False
    trusted_by: frozenset[str] = frozenset()  # provider keys
    observed_by: frozenset[str] = frozenset()  # provider keys
    last_activity: float = 0.0  # newest session mtime across providers
    dir_exists: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "trusted_by", frozenset(self.trusted_by))
        object.__setattr__(self, "observed_by", frozenset(self.observed_by))


def compute_membership(
    *,
    claude_projects: Mapping[str, object] | None,
    provider_trust: Mapping[str, Iterable[str]],
    sessions: Iterable[Session],
    pinned: AbstractSet[str],
    hidden: AbstractSet[str],
    now: float,
) -> list[MembershipEntry]:
    """Pure membership computation over the three evidence tiers + curation.

    `claude_projects=None` means the claude.json evidence could not be read:
    Claude then contributes nothing (typed evidence stays with the caller),
    while the other sources stand on their own.
    """
    session_rows = tuple(sessions)
    decay_floor = now - OBSERVED_DECAY_DAYS * _SECONDS_PER_DAY
    pinned_dirs = {os.path.normpath(p) for p in pinned}
    hidden_dirs = {os.path.normpath(p) for p in hidden}

    trusted: dict[str, set[str]] = {}
    observed: dict[str, set[str]] = {}
    latest = _latest_activity(session_rows)

    # Trusted tier — recorded candidates only; inheritance qualifies a key's
    # trust status, it never enumerates the key's descendants.
    if claude_projects is not None:
        for key in claude_projects:
            if (
                isinstance(key, str)
                and key.startswith("/")
                and effective_trust_decision(key, claude_projects)
                is TrustDecision.TRUSTED
            ):
                trusted.setdefault(os.path.normpath(key), set()).add("claude")
    for provider, directories in provider_trust.items():
        for directory in directories:
            if directory.startswith("/"):
                trusted.setdefault(os.path.normpath(directory), set()).add(provider)

    # Observed tier — session cwd activity from ANY provider.
    for session in session_rows:
        if not session.cwd:
            continue
        directory = os.path.normpath(session.cwd)
        observed.setdefault(directory, set()).add(session.provider)

    # Claude effective trust qualifies every candidate — qualification, not
    # generation. Provenance stays on the model even though the Projects view
    # no longer renders a badge column.
    if claude_projects is not None:
        for directory in set(trusted) | set(observed):
            if "claude" not in trusted.get(directory, set()) and (
                effective_trust_decision(directory, claude_projects)
                is TrustDecision.TRUSTED
            ):
                trusted.setdefault(directory, set()).add("claude")

    entries: list[MembershipEntry] = []
    candidates = set(trusted) | set(observed) | pinned_dirs | hidden_dirs
    for directory in sorted(candidates):
        trusted_by = frozenset(trusted.get(directory, ()))
        observed_by = frozenset(observed.get(directory, ()))
        activity = latest.get(directory, 0.0)
        dir_exists = os.path.isdir(directory)
        if directory in hidden_dirs:
            # Hidden wins over every tier; the entry survives decay AND
            # hygiene so the show-hidden mode can always offer the unhide
            # verb. (Pin+hidden is unreachable except via hand-editing, and
            # hidden wins there too.)
            entries.append(
                MembershipEntry(
                    directory,
                    hidden=True,
                    trusted_by=trusted_by,
                    observed_by=observed_by,
                    last_activity=activity,
                    dir_exists=dir_exists,
                )
            )
            continue
        is_pinned = directory in pinned_dirs
        if not (is_pinned or trusted_by or activity >= decay_floor):
            continue  # observed-only evidence, decayed
        if not is_pinned and (not dir_exists or _is_temp_path(directory)):
            continue  # hygiene: dead residue / working space
        entries.append(
            MembershipEntry(
                directory,
                pinned=is_pinned,
                trusted_by=trusted_by,
                observed_by=observed_by,
                last_activity=activity,
                dir_exists=dir_exists,
            )
        )
    return entries


def _basename(path: str) -> str:
    """Display name derived from the path — NEVER an identity key."""
    return os.path.basename(path.rstrip("/")) or path


@dataclass(frozen=True)
class ProjectsScan:
    """Project rows plus the non-fatal degradation of their evidence sources."""

    projects: tuple[Project, ...] = ()
    # Non-fatal degradation of the membership sources (claude.json, codex/kimi
    # trust stores, the curation file) — narrowed sources, never a blank tab.
    issues: tuple[InventoryIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "projects", tuple(self.projects))
        object.__setattr__(self, "issues", tuple(self.issues))


def scan_projects(sessions: Sequence[Session]) -> ProjectsScan:
    """Discover member projects from every evidence source, in one pass.

    ONE claude.json load feeds both the trust tier and candidate records — no
    per-project re-parse. Expected source failures (unreadable claude.json,
    broken provider trust store, malformed curation file) become typed issues
    so the tab narrows honestly instead of silently showing less.
    """
    settings = read_project_settings(cfg.claude_json)
    trust_scan = providers.scan_trusted_dirs()
    curation = read_curation(cfg.curation_file)
    entries = compute_membership(
        claude_projects=settings.projects if settings.available else None,
        provider_trust=trust_scan.directories,
        sessions=sessions,
        pinned=curation.pinned,
        hidden=curation.hidden,
        now=time.time(),
    )
    projects = [
        Project(
            name=_basename(entry.directory),
            directory=entry.directory,
            dir_exists=entry.dir_exists,
            pinned=entry.pinned,
            hidden=entry.hidden,
            trusted_by=entry.trusted_by,
            observed_by=entry.observed_by,
        )
        for entry in entries
    ]
    issues = list(trust_scan.issues)
    if not settings.available:
        detail = settings.detail or settings.state.value
        issues.append(
            InventoryIssue(
                "claude.json project settings",
                os.fspath(cfg.claude_json),
                detail,
            )
        )
    if not curation.available:
        issues.append(
            InventoryIssue(
                "curation",
                os.fspath(cfg.curation_file),
                curation.detail or curation.state.value,
            )
        )
    return ProjectsScan(tuple(projects), tuple(issues))


def order_by_activity(
    projects: Sequence[Project],
    sessions: Sequence[Session],
) -> list[Project]:
    """Pinned projects first, then newest exact-cwd activity, then path."""

    latest = _latest_activity(sessions)
    return sorted(
        projects,
        key=lambda project: (
            not project.pinned,
            -latest.get(os.path.normpath(project.directory), 0.0),
            project.directory,
        ),
    )
