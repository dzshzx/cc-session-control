"""Pure typed outcomes shared by the tmux adapter and its callers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import NamedTuple


class TmuxWindow(NamedTuple):
    """One window of a session, with its identity metadata.

    `wid` is tmux's server-unique `@N` window id — THE collision-safe address
    for kill/capture (window NAMES are cosmetic and may collide; tmux `-t` by
    name falls back to prefix matching, which can hit the wrong window).
    `path` is the project directory the window belongs to: the `@csctl_path`
    window option when csctl declared it at spawn, else `pane_current_path`
    (adopts pre-0.7.3 windows and hand-made ones). `pid` is the pane root pid
    (the hosted process itself — spawns use `exec`, replacing the shell).
    """

    wid: str
    name: str
    dead: bool
    pid: int | None
    path: str


@dataclass(frozen=True)
class TmuxIssue:
    """One expected tmux inventory failure."""

    source: str
    detail: str


@dataclass(frozen=True)
class WindowInventory:
    """Known windows plus whether tmux proved the inventory complete."""

    records: tuple[TmuxWindow, ...] = ()
    issues: tuple[TmuxIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


class PaneCaptureIssue(NamedTuple):
    """Expected failure to capture one pane."""

    source: str
    target: str
    detail: str


class PaneCaptureResult(NamedTuple):
    """Bounded pane text or typed failure evidence."""

    target: str
    text: str = ""
    issue: PaneCaptureIssue | None = None
    truncated: bool = False

    @property
    def success(self) -> bool:
        return self.issue is None


class KillState(Enum):
    """Observable outcome of killing one tmux target."""

    KILLED = "killed"
    TARGET_NOT_FOUND = "target-not-found"
    FAILED = "failed"


@dataclass(frozen=True)
class KillResult:
    state: KillState
    target: str
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is KillState.KILLED


class TmuxWriteOperation(Enum):
    """One operator-requested tmux mutation."""

    CREATE_TARGET = "create-target"
    SET_WINDOW_OPTION = "set-window-option"


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
    """Typed tmux write outcome with the created target when one exists."""

    operation: TmuxWriteOperation
    stage: TmuxWriteStage
    state: TmuxWriteState
    target: str | None = None
    detail: str = ""

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
    """Build one create result from invocation primitives."""

    if returncode is None or returncode != 0:
        return TmuxWriteResult(
            TmuxWriteOperation.CREATE_TARGET,
            stage,
            TmuxWriteState.FAILED,
            detail=detail,
        )
    target = stdout.strip()
    if not target:
        return TmuxWriteResult(
            TmuxWriteOperation.CREATE_TARGET,
            stage,
            TmuxWriteState.FAILED,
            detail="tmux succeeded without printing the created target",
        )
    return TmuxWriteResult(
        TmuxWriteOperation.CREATE_TARGET,
        stage,
        TmuxWriteState.SUCCEEDED,
        target=target,
    )


def window_option_result(
    target: str,
    returncode: int | None,
    detail: str,
) -> TmuxWriteResult:
    """Build one per-window metadata result from invocation primitives."""

    state = TmuxWriteState.SUCCEEDED if returncode == 0 else TmuxWriteState.FAILED
    return TmuxWriteResult(
        TmuxWriteOperation.SET_WINDOW_OPTION,
        TmuxWriteStage.WINDOW_OPTION,
        state,
        target,
        "" if state is TmuxWriteState.SUCCEEDED else detail,
    )


class TmuxPane(NamedTuple):
    """One pane root pid and its enterable session/window target."""

    target: str
    pid: int


@dataclass(frozen=True)
class ResidencyIssue:
    """One source preventing a complete tmux-residency inventory."""

    source: str
    path: str | None
    detail: str


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


@dataclass(frozen=True)
class SessionWindowResult:
    """First matching pane target plus evidence completeness."""

    target: str | None = None
    issues: tuple[ResidencyIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues
