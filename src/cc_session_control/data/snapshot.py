"""Shared world snapshot — ONE scan per refresh cycle (R11 / D8).

The async refresh used to call `fetch_pending()` on every view, so three tabs
each re-scanned `/proc`, the transcripts, and the registries. `build_world_snapshot`
computes that world ONCE on the worker thread; `App` then hands the same
immutable snapshot to each view's `fetch_pending(snapshot)` so they only project
it (no per-view IO). Views stay back-compatible: `fetch_pending(None)` self-fetches.

This is the TOP of the data layer — it composes `sessions` / `rc` / `liveness` /
`environments`. Nothing in `data/` imports it (only `app`/`views` do), so there
is no cycle. Recoverable external failures are typed by their owning data
module; `App` additionally guards the snapshot boundary so a failed build
degrades to per-view self-fetch.
"""

from __future__ import annotations

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
    agent_jobs: list[AgentJob] = field(default_factory=list)
    rc_projects: list[RCProject] = field(default_factory=list)
    rc_project_settings: ProjectSettingsResult = field(
        default_factory=lambda: ProjectSettingsResult(
            ProjectSettingsState.MISSING, {},
        ),
    )
    rc_servers: list[RCServer] = field(default_factory=list)
    observed_envs: list[EnvRecord] = field(default_factory=list)
    file_referenced_envs: list[EnvRecord] = field(default_factory=list)
    environment_reconciliation: environments.Reconciliation = field(
        default_factory=environments.Reconciliation,
    )
    session_procs: list[SessionProc] = field(default_factory=list)
    agents_map: dict[str, int | None] = field(default_factory=dict)
    cur: set[int] = field(default_factory=set)


def build_world_snapshot() -> WorldSnapshot:
    """Compute the shared per-cycle world once (worker thread, R11/D8).

    Heavy scans (transcript glob via `sessions.scan`, `/proc` walk via
    `rc.scan_servers`) run exactly once here instead of once per tab. The
    registry reads are ~5s-TTL cached so the few repeat reads inside `scan()`
    hit the cache. Each data owner handles its expected external failures.
    """
    session_procs, cur, agent_jobs, agents_map = liveness.liveness_inputs()
    all_sessions = sessions.scan()
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
    recon = environments.reconcile(session_procs, agent_jobs, rc_servers)
    return WorldSnapshot(
        sessions=all_sessions,
        agent_jobs=agent_jobs,
        rc_projects=rc_scan.projects,
        rc_project_settings=rc_scan.settings,
        rc_servers=rc_servers,
        observed_envs=recon.observed,
        file_referenced_envs=recon.file_referenced,
        environment_reconciliation=recon,
        session_procs=session_procs,
        agents_map=agents_map,
        cur=cur,
    )
