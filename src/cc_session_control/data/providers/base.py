"""Provider layer types — the CLI seam (ADR-0005).

A provider adapts ONE agent CLI (Claude Code, Codex CLI, Kimi Code) to the
workbench: it owns argv synthesis (resume / new session), declares typed
capabilities the UI consults, and — for non-Claude CLIs — owns disk session
discovery. Claude's discovery deliberately does NOT flow through the narrow
`DiskDiscovery` protocol: the existing `data/` pipeline (transcripts +
registry + `claude agents --json`) predates and exceeds it, and remains the
Claude provider's engine, composed by `data.snapshot`. The protocols here
cover what actions and views need uniformly across CLIs.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ...models import InventoryIssue, Session
from ..proc import ProcCliInventory


class LivenessGrade(Enum):
    """How strong a provider's pid↔session binding evidence can get.

    FULL: registry + targeted /proc probes (Claude — `sessions/<pid>.json`
    carries `procStart`). ARGV: /proc cmdline scan binds only processes whose
    argv carries the session id (`codex resume <sid>` / `kimi -S <sid>`) —
    exact for workbench-dispatched sessions, blind to bare TUIs. NONE: no
    binding; sessions are resumable records, never kill targets.
    """

    FULL = "full"
    ARGV = "argv"
    NONE = "none"


@dataclass(frozen=True)
class ProviderCaps:
    """Typed verb gates — a capability a provider lacks is absent/refused
    with a typed detail, never emulated (ADR-0005).

    Consumed today: `fork` (Sessions `f`), `cleanup` (Sessions `d` + the
    data-boundary refusal in `cleanup.remove_session`), `takeover`
    (`resolve_argv_execution` refuses providers without it).
    `background_agents` / `remote_control` are declarative: the Agents and
    Projects-RC tabs read Claude-specific data sources directly, so these
    document the contract a future non-Claude surface must consult rather
    than gating an existing verb."""

    fork: bool = False
    takeover: bool = False
    background_agents: bool = False
    remote_control: bool = False
    cleanup: bool = False
    liveness: LivenessGrade = LivenessGrade.NONE


@dataclass(frozen=True)
class ProviderScan:
    """One provider's disk-discovery result: rows plus incompleteness evidence.

    Issues are non-fatal to the refresh generation (a missing `~/.codex` must
    not blank the Claude view); they surface as degraded-source detail.
    """

    sessions: tuple[Session, ...] = ()
    issues: tuple[InventoryIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def complete(self) -> bool:
        return not self.issues


@runtime_checkable
class AgentProvider(Protocol):
    """Identity, capabilities, and argv synthesis for one agent CLI."""

    key: str  # stable id, also `Session.provider` ("claude" / "codex" / "kimi")
    label: str  # short display tag for the CLI column (e.g. "cc" / "cx" / "km")
    caps: ProviderCaps

    def available(self) -> bool:
        """Whether this CLI's state home exists on this machine."""
        ...

    def resume_argv(self, sid: str, fork: bool = False) -> list[str]:
        """The exec/tmux argv resuming `sid`. Callers gate `fork` on
        `caps.fork` first; providers raise on an unsupported fork rather than
        silently dropping it."""
        ...

    def new_session_argv(self) -> list[str]:
        """The argv starting a fresh session in the caller-chosen cwd."""
        ...

    def window_name(self, sid: str, fork: bool = False) -> str:
        """The per-session tmux window name (provider-prefixed for
        non-Claude so operators can tell CLIs apart at a glance)."""
        ...


@runtime_checkable
class ArchiveVerbs(Protocol):
    """Providers whose CLI owns an official archived-session store
    (`codex archive/unarchive <SESSION>`).

    Discovery marks rows from that store with `Session.archived`; the resume
    family refuses them honestly and hands back `unarchive_argv` instead —
    resuming straight from an archived store is unverified upstream
    semantics, so csctl offers the official recovery step rather than
    gambling (the ADR-0005 capability discipline).
    """

    def unarchive_argv(self, sid: str) -> list[str]:
        """The official argv restoring `sid` from the archived store."""
        ...


@runtime_checkable
class DiskDiscovery(Protocol):
    """Disk session discovery for providers without a claude-grade engine.

    `discover` consumes the SHARED per-generation `/proc` argv inventory
    (one walk serves every provider; extractors are pure) plus the csctl
    ancestor pid set for current-session protection.
    """

    basename: str  # argv0 basename this CLI's processes carry

    def discover(
        self,
        cli_inventory: ProcCliInventory,
        cur: AbstractSet[int],
    ) -> ProviderScan: ...
