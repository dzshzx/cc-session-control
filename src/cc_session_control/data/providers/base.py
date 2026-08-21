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

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ...models import InventoryIssue, Session
from ..proc import ProcCliInventory
from ..tmux_outcomes import PaneInventory


class LivenessGrade(Enum):
    """How strong a provider's pid↔session binding evidence can get.

    FULL: registry + targeted /proc probes (Claude — `sessions/<pid>.json`
    carries `procStart`). ARGV: /proc cmdline scan binds only processes whose
    argv carries the session id (`codex resume <sid>` / `kimi -S <sid>`), blind
    to bare TUIs. TMUX: ARGV supplemented by a provider runtime registry
    and/or csctl's own dispatch metadata. Kimi's opt-in official hooks can
    bind a session after it materializes, including a bare TUI. Dispatch
    metadata binds through the `@csctl_sid`/`@csctl_provider` options csctl
    declared at spawn (pane process identity-checked via argv0/comm/exe).
    These supplements also survive a CLI rewriting its process title at
    runtime (kimi 0.31.1), destroying cmdline evidence; bare TUIs without
    registry or metadata evidence stay blind.
    NONE: no binding; sessions are resumable records, never kill targets.
    """

    FULL = "full"
    ARGV = "argv"
    TMUX = "tmux"
    NONE = "none"


@dataclass(frozen=True)
class ProviderCaps:
    """Typed verb gates — a capability a provider lacks is absent/refused
    with a typed detail, never emulated (ADR-0005).

    Consumed today: `fork` (Sessions `f`), `cleanup` (Sessions `d` + the
    data-boundary refusal in `cleanup.remove_session`), `takeover`
    (`resolve_argv_execution` refuses providers without it)."""

    fork: bool = False
    takeover: bool = False
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

    # Read-only members: a provider may satisfy these with a plain class
    # attribute (single-instance CLIs) or a property (codex derives both
    # from its per-instance identity), so they are declared as properties —
    # a settable variable member would forbid the property form.

    @property
    def window_tag(self) -> str:
        """Leaf used in the launcher's tmux window name. Distinct from `key`
        because a multi-instance key carries `:`, which IS tmux target syntax
        (`session:window`) and would break addressing (ADR-0008)."""
        ...

    @property
    def env(self) -> Mapping[str, str]:
        """Environment every synthesized command for this provider must
        carry — empty for a CLI with one state home, `CODEX_HOME=<home>` for
        an operator-declared codex identity (ADR-0008). Consumers inject it
        at their own boundary: tmux `new-window -e`, `os.environ` before
        `execvp`, a leading assignment in a copied shell command. An empty
        mapping produces byte-identical commands to pre-0.8.7."""
        ...

    @property
    def launch_env(self) -> Mapping[str, str]:
        """Environment for actually SPAWNING this provider's process: the
        identity `env` plus any launch-only secrets that must reach the
        process yet must NEVER appear in a copied command string. Equals
        `env` for every provider except a declared codex instance with an
        `env_file` (ADR-0012). Real spawn boundaries (execvp, tmux `-e`, the
        delete subprocess) use THIS; the clipboard `env_prefix` uses `env`,
        keeping secrets out of copied commands."""
        ...

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


class CliDeleteState(Enum):
    """Outcome of one delegated official-CLI session deletion."""

    DELETED = "deleted"
    REFUSED = "refused"  # a protection verdict — the CLI never ran
    FAILED = "failed"  # the CLI ran and failed, or could not be invoked


class CliDeleteStage(Enum):
    """Where a delegated deletion's outcome was determined."""

    EVIDENCE = "evidence"  # ancestors / argv inventory / discovery incomplete
    PROTECTION = "protection"  # live / current / archived / missing verdict
    INVOKE = "invoke"  # the CLI could not be started or did not finish
    CLI = "cli"  # the CLI ran to completion


@dataclass(frozen=True)
class CliDeleteResult:
    """Typed outcome of one delegated deletion (stage + exit evidence kept)."""

    state: CliDeleteState
    stage: CliDeleteStage
    detail: str = ""
    returncode: int | None = None

    @property
    def success(self) -> bool:
        return self.state is CliDeleteState.DELETED


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
class DeleteVerbs(Protocol):
    """Providers whose CLI owns an official by-id session deletion
    (`codex delete <SESSION>` — verified 0.146.0).

    csctl shells out to that verb for Sessions `d` on a dead non-archived
    row, so deletion authority stays with the owning CLI; csctl's own
    removal seam keeps refusing non-Claude state (`cleanup.remove_session`
    — the delegation runs BESIDE that boundary, never through it). A
    provider without this protocol (kimi 0.31.1 has no delete subcommand)
    keeps the honest refusal — the verb is never emulated (ADR-0005).
    """

    def delete_session_result(self, sid: str) -> CliDeleteResult:
        """Run the official delete, bounded, keeping typed failure evidence."""
        ...


@runtime_checkable
class DiskDiscovery(Protocol):
    """Disk session discovery for providers without a claude-grade engine.

    `discover` consumes the SHARED per-generation `/proc` argv inventory
    (one walk serves every provider; extractors are pure) plus the csctl
    ancestor pid set for current-session protection, plus — C1 — the shared
    pane inventory carrying each window's dispatch metadata (None = no tmux
    evidence this call; metadata bindings then simply stay absent).
    """

    basename: str  # argv0 basename this CLI's processes carry
    # Every argv0 basename the /proc walk must net for this CLI — a superset
    # of `basename` when the runtime rewrites its own title (kimi collapses
    # cmdline to `kimi-code` or bare `kimi`, both live within one version by
    # launch path); identity is re-verified per record.
    capture_basenames: frozenset[str]
    # Environment variables the walk must capture for this CLI's processes
    # (ADR-0008): codex needs `CODEX_HOME` to tell two identities running the
    # same binary apart. Empty for a CLI csctl models with one state home.
    env_keys: frozenset[str]

    def discover(
        self,
        cli_inventory: ProcCliInventory,
        cur: AbstractSet[int],
        panes: PaneInventory | None = None,
    ) -> ProviderScan: ...


@dataclass(frozen=True)
class TrustScan:
    """One provider's trust-store read: directories plus degradation evidence.

    A MISSING store is not an issue (a fresh install has no trust records);
    an unreadable or malformed one surfaces as a non-fatal issue and narrows
    only its own source (AGENTS.md 外部失败).
    """

    directories: tuple[str, ...] = ()
    issues: tuple[InventoryIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "directories", tuple(self.directories))
        object.__setattr__(self, "issues", tuple(self.issues))


@runtime_checkable
class TrustDiscovery(Protocol):
    """Providers whose CLI keeps an explicit per-directory trust store.

    Membership evidence only (ADR-0007): the directories the operator told
    THIS CLI to trust, read exactly as recorded (no inheritance re-derivation
    — codex/kimi upstream inheritance semantics are unverified). Claude does
    NOT implement this protocol: its trust store is `~/.claude.json`, read
    through `data.project_settings` — the single typed reader membership
    discovery uses. csctl never writes any provider's trust store.
    """

    def trusted_dirs(self) -> TrustScan:
        """The CLI's recorded trusted directories plus read-failure evidence."""
        ...
