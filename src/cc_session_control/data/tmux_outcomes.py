"""Pure typed outcomes shared by the tmux adapter and its callers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import NamedTuple

from ..models import InventoryIssue

#: Same canonical (source, path, detail) issue record — path is None for tmux.
TmuxIssue = InventoryIssue


class TmuxWriteStage(Enum):
    """The exact tmux boundary reached by one write."""

    SESSION_PROBE = "session-probe"
    NEW_WINDOW = "new-window"
    NEW_SESSION = "new-session"
    WINDOW_OPTION = "window-option"


class TmuxWriteState(Enum):
    """Whether the requested tmux write completed."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class TmuxWriteResult:
    """Typed tmux write outcome with the created target when one exists.

    `target` is the enterable `session:index` the attach/notify consumers
    use; `window_id` (spawn stages only) is the server-unique `@N` id — THE
    collision-safe address for follow-up window writes, since a racing
    kill/create can reassign a name:index to a different window."""

    stage: TmuxWriteStage
    state: TmuxWriteState
    target: str | None = None
    detail: str = ""
    window_id: str | None = None

    @property
    def success(self) -> bool:
        return self.state is TmuxWriteState.SUCCEEDED

    @property
    def diagnostic(self) -> str:
        """Operator-facing stage plus honest external failure detail."""

        if self.success:
            return ""
        detail = self.detail or "tmux operation failed without diagnostic output"
        return f"{self.stage.value}: {detail}"


def create_target_result(
    stage: TmuxWriteStage,
    returncode: int | None,
    stdout: str,
    detail: str,
) -> TmuxWriteResult:
    """Build one create result from invocation primitives.

    Spawn `-P -F` printouts are `<session>:<index>\\t<window_id>` — the
    enterable target plus the server-unique window id. A printout without
    the window-id column leaves `window_id` None, so follow-up window
    writes fail safe instead of falling back to name:index addressing."""

    if returncode is None or returncode != 0:
        return TmuxWriteResult(
            stage,
            TmuxWriteState.FAILED,
            detail=detail,
        )
    target, _, window_id = stdout.strip().partition("\t")
    target = target.strip()
    if not target:
        return TmuxWriteResult(
            stage,
            TmuxWriteState.FAILED,
            detail="tmux succeeded without printing the created target",
        )
    return TmuxWriteResult(
        stage,
        TmuxWriteState.SUCCEEDED,
        target=target,
        window_id=window_id.strip() or None,
    )


def window_option_result(
    target: str,
    returncode: int | None,
    detail: str,
) -> TmuxWriteResult:
    """Build one per-window metadata result from invocation primitives."""

    state = TmuxWriteState.SUCCEEDED if returncode == 0 else TmuxWriteState.FAILED
    return TmuxWriteResult(
        TmuxWriteStage.WINDOW_OPTION,
        state,
        target,
        "" if state is TmuxWriteState.SUCCEEDED else detail,
    )


class TmuxPane(NamedTuple):
    """One pane root pid and its enterable session/window target.

    `sid`/`provider` are the window's `@csctl_sid`/`@csctl_provider` user
    options (C1): the dispatch identity csctl itself declared at spawn, "" on
    windows without it. They feed the tmux-metadata liveness binding for CLIs
    whose processes rewrite their own argv (kimi title rewrite); window NAMES
    stay cosmetic and never bind."""

    target: str
    pid: int
    sid: str = ""
    provider: str = ""


#: Same (source, path, detail) record everywhere — one canonical issue type.
ResidencyIssue = InventoryIssue


@dataclass(frozen=True)
class PaneInventory:
    """Known panes plus whether tmux proved the global list complete."""

    records: tuple[TmuxPane, ...] = ()
    issues: tuple[ResidencyIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ResidencyInventory:
    """Known pid-to-target joins plus completeness across tmux and proc."""

    targets: Mapping[int, str] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    issues: tuple[ResidencyIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def complete(self) -> bool:
        return not self.issues

    @property
    def issue_detail(self) -> str:
        """THE one `source (path): detail; …` rendering of residency issues —
        every injector (claude scan, provider snapshot merge, execution-time
        re-resolution) reads this instead of re-joining inline."""
        return "; ".join(
            f"{issue.source}"
            + (f" ({issue.path})" if issue.path else "")
            + f": {issue.detail}"
            for issue in self.issues
        )


@dataclass(frozen=True)
class SessionWindowResult:
    """First matching pane target plus evidence completeness."""

    target: str | None = None
    issues: tuple[ResidencyIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues
