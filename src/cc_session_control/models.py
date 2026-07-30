"""Data models for cc-session-control."""

from __future__ import annotations

import os.path
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

# Single source of truth for RC status values. The Chinese display labels
# (views/rc.py) and the CLI icons (cli.py) are presentation-only maps keyed
# off this vocabulary.
Status = Literal["running", "dead", "stopped", "unknown"]


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


class TrustDecision(Enum):
    """Effective Claude project trust, including unavailable evidence."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    UNAVAILABLE = "unavailable"


class RCStartupSettingState(Enum):
    """Effective per-project ``remoteControlAtStartup`` read state."""

    TRUE = "true"
    FALSE = "false"
    UNSET = "unset"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    INVALID = "invalid"


@dataclass(frozen=True)
class RCStartupSettingRead:
    """Typed evidence for one effective per-project startup setting."""

    state: RCStartupSettingState
    source: Path | None = None
    detail: str = ""

    @property
    def value(self) -> bool | None:
        if self.state is RCStartupSettingState.TRUE:
            return True
        if self.state is RCStartupSettingState.FALSE:
            return False
        return None

    @property
    def available(self) -> bool:
        return self.state in {
            RCStartupSettingState.TRUE,
            RCStartupSettingState.FALSE,
            RCStartupSettingState.UNSET,
            RCStartupSettingState.MISSING,
        }


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
    # registry `procStart` of the chosen pid — lets kill-time `proc.probe_pid`
    # rechecks defeat pid reuse ("" = unknown, recheck degrades to existence).
    proc_start: str = ""
    hidden: frozenset[str] = frozenset()
    file: str = ""
    # Unified-workbench fields (all default so existing construction stays valid).
    kind: str = ""  # registry `kind` (e.g. interactive / bg)
    entrypoint: str = ""  # registry `entrypoint` (cli / claude-vscode / sdk-ts)
    source: str = ""  # coarse bucket: cli / vscode / sdk / bg
    rc_exposed: bool = False  # session remote control currently exposed
    agent_short: str | None = None  # linked background-agent short id, if any
    status: str = ""  # registry `status` (busy / idle)
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
class AgentJob:
    """One `jobs/<short>/state.json` background-agent record.

    state.json carries NO pid; `host_pid`/`host_alive` are filled later by
    joining `sid -> sessions/<pid>.json` (see Phase 6).
    """

    short: str
    sid: str
    resume_sid: str
    state: str = ""
    tempo: str = ""
    cwd: str = ""
    name: str = ""
    env_suffix: str = ""  # suffix of the cse_* bridge id
    respawn_flags: tuple[str, ...] = ()
    host_pid: int | None = None
    host_alive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "respawn_flags", tuple(self.respawn_flags))


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
class RCProject:
    name: str
    directory: str
    # THE sole trust representation — unavailable evidence stays distinct from
    # untrusted rather than collapsing into a plain bool.
    trust_decision: TrustDecision
    in_list: bool
    status: Status
    auto_start: bool
    rc_at_startup_setting: RCStartupSettingRead = field(
        default_factory=lambda: RCStartupSettingRead(RCStartupSettingState.MISSING)
    )
    spawn_mode: str | None = None  # per-project remoteControlSpawnMode (None=unset)
    # False when the workspace directory is gone but claude.json / rc-enabled
    # still reference the project — shown as 缺失, start-ops refused.
    dir_exists: bool = True

    @property
    def rc_at_startup(self) -> bool | None:
        """Compatibility view of the typed per-project settings evidence."""

        return self.rc_at_startup_setting.value


@dataclass(frozen=True)
class RCServer:
    """A project RC server process (`claude remote-control --name`) — R5/D5.

    Discovered from tmux (managed) and/or `/proc` (external). `managed` is True
    when the pid belongs to a csctl-managed tmux pane; otherwise the server was
    started outside csctl and is READ-ONLY (no takeover/restart — review gate).
    """

    name: str
    cwd: str = ""
    managed: bool = False
    pid: int | None = None
    status: Status = "stopped"


def effective_trust_decision(
    path: str,
    projects: Mapping[str, object] | None,
) -> TrustDecision:
    """PURE: is `path` trusted per Claude Code's runtime trust-dialog gate?

    THE one trust predicate — membership discovery and the `start_one_result` gate
    both call it, never re-derive. Mirrors claude's own behavior (verified on
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


def split_env_id(value: str | None) -> tuple[str, str]:
    """`cse_abc` -> ("cse", "abc"); ("", "") when not a namespaced id.

    THE one parser for namespaced bridge/env ids (`session_*` / `cse_*`) —
    registry routes through it so the edge rules (None, no underscore, empty
    prefix or suffix) cannot diverge.
    """
    if not value:
        return "", ""
    prefix, sep, suffix = value.partition("_")
    if not sep or not prefix or not suffix:
        return "", ""
    return prefix, suffix
