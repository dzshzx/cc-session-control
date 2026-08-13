"""Stable RC outcomes and their inventory-evidence constructors."""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..models import (
    InventoryIssue,
    RCProject,
    RCServer,
    Session,
    TrustDecision,
)
from . import proc, tmux
from .project_settings import ProjectSettingsResult


@dataclass(frozen=True)
class RCScanResult:
    """Project rows plus the settings evidence used to derive trust."""

    projects: list[RCProject]
    settings: ProjectSettingsResult
    issues: tuple[InventoryIssue, ...] = ()
    # Non-fatal degradation of the ADR-0007 membership sources (codex/kimi
    # trust stores, the curation file) — narrowed sources, never a blank tab.
    membership_issues: tuple[InventoryIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class RCServerScanResult:
    """Known RC servers plus tmux and `/proc` inventory completeness."""

    servers: tuple[RCServer, ...] = ()
    issues: tuple[InventoryIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ProjectTrustResult:
    """One trust decision and the settings evidence behind it."""

    decision: TrustDecision
    settings: ProjectSettingsResult


class StartState(Enum):
    """Observable outcome of starting one project RC server."""

    STARTED = "started"
    NOT_DIRECTORY = "not-directory"
    TRUST_UNAVAILABLE = "trust-unavailable"
    UNTRUSTED = "untrusted"
    ALREADY_RUNNING = "already-running"
    INVENTORY_UNAVAILABLE = "inventory-unavailable"
    STOP_FAILED = "stop-failed"
    TMUX_FAILED = "tmux-failed"
    METADATA_FAILED = "metadata-failed"


@dataclass(frozen=True)
class StartResult:
    state: StartState
    path: str
    detail: str = ""
    issues: tuple[InventoryIssue, ...] = ()
    target: str | None = None

    @property
    def success(self) -> bool:
        return self.state is StartState.STARTED


def start_from_tmux(
    state: StartState,
    path: str,
    result: tmux.TmuxWriteResult,
) -> StartResult:
    """Project one typed tmux write into the RC start domain."""

    return StartResult(
        state,
        path,
        "" if result.success else result.diagnostic,
        target=result.target,
    )


def remote_control_command(path: str, name: str) -> str:
    """Build one explicit RC launch; each fresh process mints a cloud env."""

    return (
        f"cd {shlex.quote(path)} && exec claude remote-control "
        f"--name {shlex.quote(name)} --spawn same-dir"
    )


class StopState(Enum):
    """Observable outcome of stopping managed project RC server(s)."""

    STOPPED = "stopped"
    NOT_RUNNING = "not-running"
    FAILED = "failed"


@dataclass(frozen=True)
class StopResult:
    state: StopState
    path: str
    detail: str = ""
    issues: tuple[InventoryIssue, ...] = ()

    @property
    def success(self) -> bool:
        return self.state is StopState.STOPPED


@dataclass(frozen=True)
class StopAllResult:
    state: StopState
    session: str
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is StopState.STOPPED


def order_by_activity(
    projects: Sequence[RCProject],
    sessions: Sequence[Session],
) -> list[RCProject]:
    """Pinned projects first, then newest exact-cwd activity, then path."""

    latest: dict[str, float] = {}
    for session in sessions:
        if session.cwd:
            key = os.path.normpath(session.cwd)
            latest[key] = max(session.mtime, latest.get(key, 0.0))
    return sorted(
        projects,
        key=lambda project: (
            not project.pinned,
            -latest.get(os.path.normpath(project.directory), 0.0),
            project.directory,
        ),
    )


def proc_inventory_issues(
    inventory: proc.ProcRCInventory,
) -> tuple[InventoryIssue, ...]:
    return tuple(
        InventoryIssue(issue.source, issue.path, issue.detail)
        for issue in inventory.issues
    )


def format_inventory_issues(issues: Sequence[InventoryIssue]) -> str:
    return "; ".join(
        f"{issue.source}"
        + (f" ({issue.path})" if issue.path else "")
        + f": {issue.detail}"
        for issue in issues
    )
