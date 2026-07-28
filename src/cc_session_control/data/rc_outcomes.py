"""Stable RC outcomes and their inventory-evidence constructors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..models import (
    InventoryIssue,
    RCProject,
    RCServer,
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


@dataclass(frozen=True)
class StartResult:
    state: StartState
    path: str
    detail: str = ""
    issues: tuple[InventoryIssue, ...] = ()

    @property
    def success(self) -> bool:
        return self.state is StartState.STARTED


@dataclass(frozen=True)
class StartManyResult:
    started: int = 0
    unavailable: int = 0
    untrusted: int = 0
    failed: int = 0


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


def format_inventory_issues(issues: Sequence[InventoryIssue]) -> str:
    return "; ".join(
        f"{issue.source}"
        + (f" ({issue.path})" if issue.path else "")
        + f": {issue.detail}"
        for issue in issues
    )
