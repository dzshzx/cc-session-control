"""Shared world snapshot — ONE scan per refresh cycle (R11 / D8).

The async refresh used to let every view scan `/proc`, transcripts, and
registries independently. `build_world_snapshot` computes that world ONCE on
the worker thread; `data.refresh.build_refresh_result` derives one complete batch
from it before the App main loop gives that batch to every view.

This is the TOP of the data layer — it composes `sessions` / `rc` / `liveness` /
`environments`. Only the data layer's top-level `refresh` module imports it, so
there is no cycle. Recoverable external failures are typed by their owning data
module. Expected boundary I/O failures become an explicit `RefreshFailure`;
programming and invariant errors are not converted into empty view data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ..models import AgentJob, EnvRecord, RCProject, RCServer, Session, SessionProc
from . import environments, liveness, rc, sessions
from .project_settings import ProjectSettingsResult, ProjectSettingsState
from .sessions import SessionScanResult


@dataclass(frozen=True)
class WorldSnapshot:
    """One cycle's shared view of the machine (read-only data for the views).

    `sessions` is the full transcript-driven scan (SessionsView), `agent_jobs`
    the background jobs enriched with host liveness (AgentsView), and
    `rc_projects`/`rc_servers` the Remote Control world (RCView). The two env
    sets are the bridge-environment ledger's two tiers (R6):
      - `observed_envs` — ALIVE-GATED (`observe_live`): the CURRENT/bound display.
      - `file_referenced_envs` — bridge-truthy (`observe`): ledger MEMBERSHIP, and
        the set orphans are computed against (`orphan = ledger − file-referenced`).

    `environment_reconciliation` carries the ledger update outcome and any
    integrity warning so the Projects status can expose degraded history.

    `session_procs` (with `/proc` liveness already injected), `agents_map`
    (`claude agents --json`) and `cur` (the ancestor-pid set) are the raw liveness
    inputs `build_world_snapshot` already computes for the scan. The owning
    `liveness_snapshot` is passed intact to `cleanup.build_plan`; the projections
    remain available to views without becoming a second completeness authority
    (R11/D8).
    """

    sessions: tuple[Session, ...] = ()
    agent_jobs: tuple[AgentJob, ...] = ()
    rc_projects: tuple[RCProject, ...] = ()
    rc_project_settings: ProjectSettingsResult = field(
        default_factory=lambda: ProjectSettingsResult(
            ProjectSettingsState.MISSING,
            {},
        ),
    )
    rc_servers: tuple[RCServer, ...] = ()
    observed_envs: tuple[EnvRecord, ...] = ()
    file_referenced_envs: tuple[EnvRecord, ...] = ()
    environment_reconciliation: environments.Reconciliation = field(
        default_factory=environments.Reconciliation,
    )
    session_procs: tuple[SessionProc, ...] = ()
    agents_map: Mapping[str, int | None] = field(default_factory=dict)
    cur: frozenset[int] = frozenset()
    liveness_snapshot: liveness.LivenessSnapshot | None = None
    transcript_scan: SessionScanResult = field(default_factory=SessionScanResult)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "agent_jobs", tuple(self.agent_jobs))
        object.__setattr__(self, "rc_projects", tuple(self.rc_projects))
        object.__setattr__(self, "rc_servers", tuple(self.rc_servers))
        object.__setattr__(self, "observed_envs", tuple(self.observed_envs))
        object.__setattr__(
            self,
            "file_referenced_envs",
            tuple(self.file_referenced_envs),
        )
        object.__setattr__(self, "session_procs", tuple(self.session_procs))
        object.__setattr__(
            self,
            "agents_map",
            MappingProxyType(dict(self.agents_map)),
        )
        object.__setattr__(self, "cur", frozenset(self.cur))


def build_world_snapshot() -> WorldSnapshot:
    """Compute the shared per-cycle world once (worker thread, R11/D8).

    Heavy scans (typed transcript discovery via `sessions.scan_result`, the
    full `/proc` walk via
    `rc.scan_servers`) run exactly once here instead of once per tab. Session
    liveness uses targeted per-pid `/proc` reads, not another full walk; those
    inputs are captured once and injected into `sessions.scan_result`. Each data
    owner handles its expected external failures.
    """
    inputs = liveness.liveness_inputs()
    transcript_scan = sessions.scan_result(inputs)
    if not transcript_scan.complete:
        return WorldSnapshot(
            sessions=transcript_scan.sessions,
            session_procs=inputs.session_procs,
            agents_map=inputs.agents_map,
            cur=inputs.cur,
            liveness_snapshot=inputs,
            transcript_scan=transcript_scan,
        )
    all_sessions = transcript_scan.sessions
    window_inventory = rc._tmux_window_inventory()
    rc_scan = rc.scan_result(window_inventory=window_inventory)
    server_scan = rc.scan_servers_result(window_inventory=window_inventory)
    rc_servers = server_scan.servers
    # R6 ledger reconciliation (the whole point of the ledger): ONE pipeline —
    # observe (file-referenced membership) → upsert → observe_live (alive-gated
    # CURRENT) — owned by `environments.reconcile`, so the ordering invariant
    # never lives here. An env that later toggles away (RC turned off / job
    # removed / server stopped) stays in the ledger but drops out of the
    # file-referenced set, surfacing as an orphan / manual-delete candidate.
    # Cheap and safe on the worker thread: the ledger write is write-on-change
    # + flock + compacted, so re-observing the same set is a no-op rewrite.
    recon = environments.reconcile(
        inputs,
        rc_servers,
        inventory_issues=server_scan.issues,
    )
    return WorldSnapshot(
        sessions=tuple(all_sessions),
        agent_jobs=inputs.agent_jobs,
        rc_projects=tuple(rc_scan.projects),
        rc_project_settings=rc_scan.settings,
        rc_servers=tuple(rc_servers),
        observed_envs=tuple(recon.observed),
        file_referenced_envs=tuple(recon.file_referenced),
        environment_reconciliation=recon,
        session_procs=inputs.session_procs,
        agents_map=inputs.agents_map,
        cur=inputs.cur,
        liveness_snapshot=inputs,
        transcript_scan=transcript_scan,
    )
