"""Data models for cc-session-control."""

from __future__ import annotations

import os.path
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class InventoryIssue:
    """One incomplete external inventory source for operator-facing results."""

    source: str
    path: str | None
    detail: str


def issue_detail(issues: Iterable[InventoryIssue]) -> str:
    """The one `source at path: detail; …` rendering of issue evidence."""
    parts = []
    for issue in issues:
        location = f" at {issue.path}" if issue.path else ""
        parts.append(f"{issue.source}{location}: {issue.detail}")
    return "; ".join(parts)


def format_inventory_issues(issues: Sequence[InventoryIssue]) -> str:
    """The one `source (path): detail; …` rendering of inventory evidence."""
    return "; ".join(
        f"{issue.source}"
        + (f" ({issue.path})" if issue.path else "")
        + f": {issue.detail}"
        for issue in issues
    )


class TrustDecision(Enum):
    """Effective Claude project trust, including unavailable evidence."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Session:
    sid: str
    cwd: str
    label: str
    mtime: float
    prompts: int
    pid: int | None
    alive: bool
    current: bool
    # Owning agent CLI (ADR-0005). Part of session identity: sids are unique
    # only WITHIN a provider, and every action dispatch (resume argv, window
    # naming, capability gates) routes through `providers.get(provider)`.
    provider: str = "claude"
    # registry `procStart` of the chosen pid — lets kill-time `proc.probe_pid`
    # rechecks defeat pid reuse ("" = unknown, recheck degrades to existence).
    proc_start: str = ""
    hidden: frozenset[str] = frozenset()
    file: str = ""
    # Unified-workbench fields (all default so existing construction stays valid).
    kind: str = ""  # registry `kind` (e.g. interactive / bg)
    entrypoint: str = ""  # registry `entrypoint` (cli / claude-vscode / sdk-ts)
    source: str = ""  # coarse bucket: cli / vscode / desktop / sdk / bg / remote
    # Discovered from the provider's archived store (codex
    # `archived_sessions/`) rather than the active tree. Archived rows stay
    # visible/searchable, but the resume family refuses them honestly with
    # the official un-archive command — resuming straight from an archived
    # store is unverified upstream semantics. The False default keeps every
    # existing construction and consumer byte-identical.
    archived: bool = False
    rc_exposed: bool = False  # session remote control currently exposed
    status: str = ""  # registry `status` (busy / idle)
    # ADR-0005 unbound-live hint (fourth status state): a live provider
    # process that argv binding could NOT tie to any sid (bare TUI / picker)
    # runs in this session's cwd, and this row is that directory's newest
    # non-alive candidate — resuming it MAY double-attach the same session.
    # Advisory only: it never upgrades `alive`, never feeds kill/takeover
    # decisions, and its False default keeps every consumer's behavior
    # byte-identical (fail-safe).
    unbound_live_hint: bool = False
    # Exact rollout-path evidence from a Codex app-server fd table. Hosted
    # sessions are present in Desktop/IDE, but they are deliberately NOT
    # `alive`: there is no session-owning pid that csctl may signal. The flag
    # is display/read-only authority only and must never feed takeover/delete.
    hosted: bool = False
    # tmux residency (CONTEXT.md / ADR-0001): non-None means a live pid of this
    # session runs inside a tmux pane; the value is the enterable
    # "session:window_index" target. Batch-computed in sessions.scan_result() via
    # tmux.residency_inventory — actions and the ⧉ badge read the SAME field.
    tmux_target: str | None = None
    # False means the global pane or per-pid ancestor inventory was incomplete:
    # absence of `tmux_target` is unknown, not proof this live session is bare.
    tmux_inventory_complete: bool = True
    tmux_inventory_detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "hidden", frozenset(self.hidden))

    @property
    def bridge_or_sdk(self) -> bool:
        """D9: union of the transcript `hidden` tags and registry source==sdk.

        The 桥接/SDK hide filter (Phase 7) keys off this so the badge and the
        `h` toggle stay consistent whether the SDK signal arrived from the
        transcript marker (`hidden`) or the registry entrypoint (`source`).
        Kept here so the two signals never contradict at the model level.
        """
        return bool(self.hidden) or self.source == "sdk"


@dataclass(frozen=True)
class SessionProc:
    """One `sessions/<pid>.json` registry entry (a session's local runtime).

    A single sessionId may have several of these — resume keeps the sid but
    mints a new pid. `proc_start` defeats pid reuse (compared to /proc stat).
    """

    pid: int
    sid: str
    cwd: str = ""
    kind: str = ""
    entrypoint: str = ""
    status: str = ""
    proc_start: str = ""  # registry `procStart` (kernel starttime, as str)
    # Injected /proc liveness, never parsed from JSON. Tri-state sentinel:
    # None = NOT YET INJECTED (only `liveness.live_session_procs` sets it) —
    # destructive consumers (`select_zombie_pids`) refuse None rather than
    # treating an uninjected row as dead, so misuse fails safe (不删) instead
    # of mass-deleting live sessions' files.
    proc_alive: bool | None = None
    bridge: str | None = None  # `bridgeSessionId` (session_* namespace)


@dataclass(frozen=True)
class LiveInfo:
    """Merged liveness/identity for one sessionId (output of live_index)."""

    sid: str
    pid: int | None = None
    proc_start: str = ""
    status: str = ""
    kind: str = ""
    entrypoint: str = ""
    bridge: str | None = None
    source: str = ""
    alive: bool = False
    proc_alive: bool = False
    # All proc-confirmed alive pids for this sid (resume mints new pids while
    # keeping the sid). `pid` is the chosen one for display; `pids` is the full
    # candidate set so "current" detection protects ANY ancestor pid, not just
    # the newest (multi-pid under-protection fix).
    pids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pids", tuple(self.pids))


@dataclass(frozen=True)
class Project:
    """One projects-tab row: an absolute directory plus membership evidence.

    The tab is a pure launcher + membership surface, so the row carries no
    runtime state — `dir_exists` marks a stale reference (the workspace
    directory is gone but membership evidence or a pin still references the
    project) and gates the launch verbs.
    """

    name: str
    directory: str
    dir_exists: bool = True
    # ADR-0007 evidence-tier provenance: `trusted_by` holds the provider keys
    # whose trust store covers this directory (claude = effective/inherited
    # trust; codex/kimi = exact-match records); `observed_by` holds providers
    # with session activity in the directory. `hidden` rows ship in the scan
    # so the view's show-hidden mode can offer the unhide verb; the view
    # filters them in normal mode.
    pinned: bool = False
    hidden: bool = False
    trusted_by: frozenset[str] = frozenset()
    observed_by: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "trusted_by", frozenset(self.trusted_by))
        object.__setattr__(self, "observed_by", frozenset(self.observed_by))


def effective_trust_decision(
    path: str,
    projects: Mapping[str, object] | None,
) -> TrustDecision:
    """PURE: is `path` trusted per Claude Code's runtime trust-dialog gate?

    THE one trust predicate — membership discovery calls it, never re-derives.
    Mirrors claude's own behavior (verified on
    2.1.218): the dialog is suppressed when the cwd or ANY ancestor entry in
    `projects` (the `~/.claude.json` map) carries `hasTrustDialogAccepted:
    true`. An inherited subdirectory gets an entry with an EXPLICIT False
    flag ("suppressed, never asked"), while declining the dialog writes no
    entry at all — so explicit False must NOT veto. Ancestor matching is by
    path-segment boundary (`/a/workspace` never covers `/a/workspace-external`)
    and normalizes with normpath only — never realpath, matching claude's
    literal-cwd record keeping.

    ``None`` means the settings evidence could not be read or validated.  That
    state is distinct from a valid project map that does not grant trust.
    """
    if projects is None:
        return TrustDecision.UNAVAILABLE
    if not path:
        return TrustDecision.UNTRUSTED
    target = os.path.normpath(path)
    for key, val in projects.items():
        if (
            not isinstance(key, str)
            or not isinstance(val, Mapping)
            or val.get("hasTrustDialogAccepted") is not True
        ):
            continue
        root = os.path.normpath(key)
        if target == root or target.startswith(root.rstrip("/") + "/"):
            return TrustDecision.TRUSTED
    return TrustDecision.UNTRUSTED
