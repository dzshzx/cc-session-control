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
    version: str = ""


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
    backend: str = ""


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
    environment_id: str = ""
