"""Stable RC outcomes and their inventory-evidence constructors."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..models import (
    InventoryIssue,
    RCProject,
    RCServer,
    TrustDecision,
)
from . import proc, rc_environment, tmux
from .project_settings import ProjectSettingsResult


@dataclass(frozen=True)
class RCScanResult:
    """Project rows plus the settings evidence used to derive trust."""

    projects: list[RCProject]
    settings: ProjectSettingsResult
    issues: tuple[InventoryIssue, ...] = ()

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
    tmux_result: tmux.TmuxWriteResult | None = None

    @property
    def success(self) -> bool:
        return self.state is StartState.STARTED


@dataclass(frozen=True)
class StartManyResult:
    started: int = 0
    unavailable: int = 0
    untrusted: int = 0
    failed: int = 0
    results: tuple[StartResult, ...] = ()


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
        tmux_result=result,
    )


def remote_control_command(path: str, name: str) -> str:
    """Build one explicit RC launch; each fresh process mints a cloud env."""

    return (
        f"cd {shlex.quote(path)} && exec claude remote-control "
        f"--name {shlex.quote(name)} --spawn same-dir"
    )


def summarize_starts(results: Sequence[StartResult]) -> StartManyResult:
    """Retain every start result alongside stable aggregate counters."""

    items = tuple(results)
    return StartManyResult(
        sum(item.state is StartState.STARTED for item in items),
        sum(item.state is StartState.TRUST_UNAVAILABLE for item in items),
        sum(item.state is StartState.UNTRUSTED for item in items),
        sum(
            item.state
            not in {
                StartState.STARTED,
                StartState.TRUST_UNAVAILABLE,
                StartState.UNTRUSTED,
            }
            for item in items
        ),
        items,
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


@dataclass(frozen=True)
class RemoveResult:
    """Enabled-list mutation plus the independent managed-window outcome."""

    list_removed: bool
    stop: StopResult


def window_inventory_issues(
    inventory: tmux.WindowInventory,
) -> tuple[InventoryIssue, ...]:
    return tuple(
        InventoryIssue(issue.source, None, issue.detail) for issue in inventory.issues
    )


def proc_inventory_issues(
    inventory: proc.ProcRCInventory,
) -> tuple[InventoryIssue, ...]:
    return tuple(
        InventoryIssue(issue.source, issue.path, issue.detail)
        for issue in inventory.issues
    )


def environment_capture_issues(
    resolution: rc_environment.EnvironmentIdResolution,
) -> tuple[InventoryIssue, ...]:
    """Translate per-window capture failures into operator inventory issues."""

    return tuple(
        InventoryIssue(
            issue.source,
            f"{issue.path} [{issue.target}]" if issue.path else issue.target,
            issue.detail,
        )
        for issue in resolution.issues
    )


def format_inventory_issues(issues: Sequence[InventoryIssue]) -> str:
    return "; ".join(
        f"{issue.source}"
        + (f" ({issue.path})" if issue.path else "")
        + f": {issue.detail}"
        for issue in issues
    )
