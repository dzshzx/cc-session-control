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
pinned and rc-window-held entries are exempt from hygiene, and hidden
entries are returned flagged (never filtered away here) so the view's
show-hidden toggle can still offer the unhide verb.

The load-bearing invariant: trust inheritance only ever QUALIFIES a recorded
candidate, it never GENERATES one — candidates come from explicit records
(trust-store keys, pins) and observed activity only, so a trusted `/` cannot
flood the tab.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from ..models import Session, TrustDecision, effective_trust_decision

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


@dataclass(frozen=True)
class MembershipEntry:
    """One candidate directory and its full provenance evidence set."""

    directory: str  # normpath'd absolute path — THE identity key
    pinned: bool = False
    hidden: bool = False
    trusted_by: frozenset[str] = frozenset()  # provider keys
    observed_by: frozenset[str] = frozenset()  # provider keys
    last_activity: float = 0.0  # newest session mtime across providers
    dir_exists: bool = True
    has_window: bool = False  # holds a live/dead RC tmux window

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
    window_paths: Iterable[str],
    now: float,
) -> list[MembershipEntry]:
    """Pure membership computation over the three evidence tiers + curation.

    `claude_projects=None` means the claude.json evidence could not be read:
    Claude then contributes nothing (typed evidence stays with the caller),
    while the other sources stand on their own.
    """
    decay_floor = now - OBSERVED_DECAY_DAYS * _SECONDS_PER_DAY
    pinned_dirs = {os.path.normpath(p) for p in pinned}
    hidden_dirs = {os.path.normpath(p) for p in hidden}
    windows = {os.path.normpath(p) for p in window_paths}

    trusted: dict[str, set[str]] = {}
    observed: dict[str, set[str]] = {}
    latest: dict[str, float] = {}

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
    for session in sessions:
        if not session.cwd:
            continue
        directory = os.path.normpath(session.cwd)
        observed.setdefault(directory, set()).add(session.provider)
        latest[directory] = max(session.mtime, latest.get(directory, 0.0))

    # The claude trust badge qualifies every candidate (effective trust is
    # what the RC start gate reads) — qualification, not generation.
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
                    has_window=directory in windows,
                )
            )
            continue
        is_pinned = directory in pinned_dirs
        if not (is_pinned or trusted_by or activity >= decay_floor):
            continue  # observed-only evidence, decayed
        has_window = directory in windows
        if (
            not is_pinned
            and not has_window
            and (not dir_exists or _is_temp_path(directory))
        ):
            continue  # hygiene: dead residue / working space
        entries.append(
            MembershipEntry(
                directory,
                pinned=is_pinned,
                trusted_by=trusted_by,
                observed_by=observed_by,
                last_activity=activity,
                dir_exists=dir_exists,
                has_window=has_window,
            )
        )
    return entries
