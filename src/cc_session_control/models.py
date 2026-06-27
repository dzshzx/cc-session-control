"""Data models for cc-session-control."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Single source of truth for RC status values. The Chinese display labels
# (views/rc.py) and the CLI icons (cli.py) are presentation-only maps keyed
# off this vocabulary.
Status = Literal["running", "dead", "stopped"]


@dataclass
class Session:
    sid: str
    cwd: str
    label: str
    mtime: float
    prompts: int
    pid: int | None
    alive: bool
    current: bool
    hidden: set[str] = field(default_factory=set)
    file: str = ""
    # Unified-workbench fields (all default so existing construction stays valid).
    kind: str = ""              # registry `kind` (e.g. interactive / bg)
    entrypoint: str = ""        # registry `entrypoint` (cli / claude-vscode / sdk-ts)
    source: str = ""            # coarse bucket: cli / vscode / sdk / bg
    rc_exposed: bool = False    # session remote control currently exposed
    env_id: str | None = None   # bound bridge environment id, if any
    agent_short: str | None = None  # linked background-agent short id, if any
    status: str = ""            # registry `status` (busy / idle)

    @property
    def bridge_or_sdk(self) -> bool:
        """D9: union of the transcript `hidden` tags and registry source==sdk.

        The 桥接/SDK hide filter (Phase 7) keys off this so the badge and the
        `h` toggle stay consistent whether the SDK signal arrived from the
        transcript marker (`hidden`) or the registry entrypoint (`source`).
        Kept here so the two signals never contradict at the model level.
        """
        return bool(self.hidden) or self.source == "sdk"


@dataclass
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
    proc_start: str = ""        # registry `procStart` (kernel starttime, as str)
    proc_alive: bool = False    # injected /proc liveness, never parsed from JSON
    bridge: str | None = None   # `bridgeSessionId` (session_* namespace)


@dataclass
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
    env_suffix: str = ""        # suffix of the cse_* bridge id
    respawn_flags: list[str] = field(default_factory=list)
    host_pid: int | None = None
    host_alive: bool = False


@dataclass
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
    pids: list[int] = field(default_factory=list)


@dataclass
class RCProject:
    name: str
    directory: str
    trusted: bool
    in_list: bool
    status: Status
    auto_start: bool
    rc_at_startup: bool | None = None  # per-project remoteControlAtStartup override
    spawn_mode: str | None = None      # per-project remoteControlSpawnMode (None=unset)


@dataclass
class RCServer:
    """A project RC server process (`claude remote-control --name`) — R5/D5.

    Discovered from tmux (managed) and/or `/proc` (external). `managed` is True
    when the pid belongs to a csctl-managed tmux pane; otherwise the server was
    started outside csctl and is READ-ONLY (no takeover/restart — review gate).
    `env_id` is the full cloud bridge id (`env_*`) captured from a managed
    server's pane output, or None when unknown / external.
    """
    name: str
    cwd: str = ""
    managed: bool = False
    pid: int | None = None
    env_id: str | None = None
    status: Status = "stopped"


@dataclass(frozen=True)
class EnvRecord:
    """One live observation of a bridge environment (R6, D4).

    `prefix` is the namespace (`session` / `cse` / `env`); `key` is the suffix
    that is the canonical environment id WITHIN that namespace. `bound_sid` is
    the session this observation is bound to (None for namespaces with no sid).
    Frozen so it is hashable for set membership when splitting current/orphan.
    Built by `environments.observe()` (registry) and, in Phase 5, pushed in by
    `rc` for the `env_*` namespace — environments never imports rc.
    """
    prefix: str
    key: str
    bound_sid: str | None = None


@dataclass
class BridgeEnv:
    """A ledger entry for one bridge environment (R6, D4).

    `status` is NOT persisted — it is recomputed against the current observation
    by `current_envs`/`orphan_envs` (an orphan is a manual-delete candidate on
    claude.ai/code; there is no local deregister). `first_seen`/`last_seen` are
    epoch seconds. The full namespaced id is `prefix_key`.
    """
    prefix: str
    key: str
    bound_sid: str | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    status: Literal["current", "orphan"] = "orphan"

    @property
    def env_id(self) -> str:
        """Full namespaced id (`cse_<key>` / `session_<key>` / `env_<key>`)."""
        return f"{self.prefix}_{self.key}"
