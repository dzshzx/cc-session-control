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

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field

from ..models import AgentJob, EnvRecord, RCProject, RCServer, Session, SessionProc
from . import environments, liveness, rc, sessions
from .project_settings import ProjectSettingsResult, ProjectSettingsState


@dataclass
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
    inputs `build_world_snapshot` already computes for the scan; they are exposed
    here so the Sessions cleanup submenu can feed `cleanup.build_plan` /
    `select_zombie_pids` WITHOUT a second scan (R11/D8).
    """

    sessions: list[Session] = field(default_factory=list)
    agent_jobs: Sequence[AgentJob] = field(default_factory=tuple)
    rc_projects: list[RCProject] = field(default_factory=list)
    rc_project_settings: ProjectSettingsResult = field(
        default_factory=lambda: ProjectSettingsResult(
            ProjectSettingsState.MISSING,
            {},
        ),
    )
    rc_servers: list[RCServer] = field(default_factory=list)
    observed_envs: list[EnvRecord] = field(default_factory=list)
    file_referenced_envs: list[EnvRecord] = field(default_factory=list)
    environment_reconciliation: environments.Reconciliation = field(
        default_factory=environments.Reconciliation,
    )
    session_procs: Sequence[SessionProc] = field(default_factory=tuple)
    agents_map: Mapping[str, int | None] = field(default_factory=dict)
    cur: AbstractSet[int] = field(default_factory=frozenset)
    liveness_snapshot: liveness.LivenessSnapshot | None = None


def build_world_snapshot() -> WorldSnapshot:
    """Compute the shared per-cycle world once (worker thread, R11/D8).

    Heavy scans (transcript glob via `sessions.scan`, the full `/proc` walk via
    `rc.scan_servers`) run exactly once here instead of once per tab. Session
    liveness uses targeted per-pid `/proc` reads, not another full walk; those
    inputs are captured once and injected into `sessions.scan`. Each data owner
    handles its expected external failures.
    """
    inputs = liveness.liveness_inputs()
    all_sessions = sessions.scan(inputs)
    rc_scan = rc.scan_result()
    rc_servers = rc.scan_servers()
    # R6 ledger reconciliation (the whole point of the ledger): ONE pipeline —
    # observe (file-referenced membership) → upsert → observe_live (alive-gated
    # CURRENT) — owned by `environments.reconcile`, so the ordering invariant
    # never lives here. An env that later toggles away (RC turned off / job
    # removed / server stopped) stays in the ledger but drops out of the
    # file-referenced set, surfacing as an orphan / manual-delete candidate.
    # Cheap and safe on the worker thread: the ledger write is write-on-change
    # + flock + compacted, so re-observing the same set is a no-op rewrite.
    recon = environments.reconcile(
        inputs.session_procs,
        inputs.agent_jobs,
        rc_servers,
    )
    return WorldSnapshot(
        sessions=all_sessions,
        agent_jobs=inputs.agent_jobs,
        rc_projects=rc_scan.projects,
        rc_project_settings=rc_scan.settings,
        rc_servers=rc_servers,
        observed_envs=recon.observed,
        file_referenced_envs=recon.file_referenced,
        environment_reconciliation=recon,
        session_procs=inputs.session_procs,
        agents_map=inputs.agents_map,
        cur=inputs.cur,
        liveness_snapshot=inputs,
    )
