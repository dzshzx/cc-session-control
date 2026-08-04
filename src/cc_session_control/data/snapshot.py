"""Shared world snapshot — ONE scan per refresh cycle (R11 / D8).

The async refresh used to let every view scan `/proc`, transcripts, and
registries independently. `build_world_snapshot` computes that world ONCE on
the worker thread; `data.refresh.build_refresh_result` derives one complete batch
from it before the App main loop gives that batch to every view.

This is the TOP of the data layer — it composes `sessions` / `rc` / `liveness`.
Only the data layer's top-level `refresh` module imports it, so there is no
cycle. Recoverable external failures are typed by their owning data module.
Expected boundary I/O failures become an explicit `RefreshFailure`;
programming and invariant errors are not converted into empty view data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..models import AgentJob, InventoryIssue, RCProject, RCServer, Session
from . import liveness, providers, rc, sessions, tmux
from .project_settings import ProjectSettingsResult, ProjectSettingsState
from .sessions import SessionScanResult


@dataclass(frozen=True)
class WorldSnapshot:
    """One cycle's shared view of the machine (read-only data for the views).

    `sessions` is the full transcript-driven scan (SessionsView), `agent_jobs`
    the background jobs enriched with host liveness (AgentsView), and
    `rc_projects`/`rc_servers` the Remote Control world (RCView).
    `rc_inventory_issues` carries the RC server scan's tmux + `/proc`
    incompleteness evidence so the Projects status can expose a degraded
    inventory.

    `liveness_snapshot` is the one typed liveness evidence `build_world_snapshot`
    already computes for the scan (`.session_procs` with `/proc` liveness already
    injected, `.agents_map` from `claude agents --json`, `.cur` the ancestor-pid
    set); it is passed intact to `cleanup.build_plan` and is the sole authority
    views/cleanup read those projections from — WorldSnapshot does not keep a
    second copy (R11/D8).
    """

    sessions: tuple[Session, ...] = ()
    # Non-fatal degradation evidence from non-Claude provider discovery
    # (ADR-0005): a broken codex/kimi source narrows the list, never blanks
    # the generation. Claude transcript issues keep failing the generation.
    provider_issues: tuple[InventoryIssue, ...] = ()
    agent_jobs: tuple[AgentJob, ...] = ()
    rc_projects: tuple[RCProject, ...] = ()
    rc_project_settings: ProjectSettingsResult = field(
        default_factory=lambda: ProjectSettingsResult(
            ProjectSettingsState.MISSING,
            {},
        ),
    )
    rc_servers: tuple[RCServer, ...] = ()
    rc_inventory_issues: tuple[InventoryIssue, ...] = ()
    liveness_snapshot: liveness.LivenessSnapshot | None = None
    transcript_scan: SessionScanResult = field(default_factory=SessionScanResult)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "provider_issues", tuple(self.provider_issues))
        object.__setattr__(self, "agent_jobs", tuple(self.agent_jobs))
        object.__setattr__(self, "rc_projects", tuple(self.rc_projects))
        object.__setattr__(self, "rc_servers", tuple(self.rc_servers))
        object.__setattr__(
            self,
            "rc_inventory_issues",
            tuple(self.rc_inventory_issues),
        )


def build_world_snapshot() -> WorldSnapshot:
    """Compute the shared per-cycle world once (worker thread, R11/D8).

    Heavy scans (typed transcript discovery via `sessions.scan_result`, the
    full `/proc` walk via
    `rc.scan_servers_result`) run exactly once here instead of once per tab. Session
    liveness uses targeted per-pid `/proc` reads, not another full walk; those
    inputs are captured once and injected into `sessions.scan_result`. Each data
    owner handles its expected external failures.
    """
    inputs = liveness.liveness_inputs()
    transcript_scan = sessions.scan_result(inputs)
    if not transcript_scan.complete:
        return WorldSnapshot(
            sessions=transcript_scan.sessions,
            liveness_snapshot=inputs,
            transcript_scan=transcript_scan,
        )
    provider_rows, provider_issues = providers.scan_non_claude(inputs.cur)
    all_sessions = _merged_sessions(transcript_scan.sessions, provider_rows)
    window_inventory = rc._tmux_window_inventory()
    rc_scan = rc.scan_result(window_inventory=window_inventory)
    server_scan = rc.scan_servers_result(window_inventory=window_inventory)
    return WorldSnapshot(
        sessions=all_sessions,
        provider_issues=provider_issues,
        agent_jobs=inputs.agent_jobs,
        rc_projects=tuple(rc_scan.projects),
        rc_project_settings=rc_scan.settings,
        rc_servers=tuple(server_scan.servers),
        rc_inventory_issues=server_scan.issues,
        liveness_snapshot=inputs,
        transcript_scan=transcript_scan,
    )


def _merged_sessions(
    claude_rows: tuple[Session, ...],
    provider_rows: tuple[Session, ...],
) -> tuple[Session, ...]:
    """Merge provider rows (residency-injected) into one mtime-ordered list.

    Claude rows already carry tmux residency from `sessions.scan_result`'s
    per-generation inventory; non-Claude alive rows get theirs from ONE
    additional targeted inventory call, made only when such pids exist at all
    (the common zero-live case adds no tmux subprocess)."""
    if not provider_rows:
        return claude_rows  # pure-Claude generation flows through uncopied
    provider_rows = _inject_provider_residency(provider_rows)
    return providers.merge_sessions(claude_rows, provider_rows)


def _inject_provider_residency(
    rows: tuple[Session, ...],
) -> tuple[Session, ...]:
    alive_pids = {row.pid for row in rows if row.alive and row.pid}
    if not alive_pids:
        return rows
    inventory = tmux.residency_inventory(alive_pids)
    return tuple(
        replace(
            row,
            tmux_target=inventory.targets.get(row.pid) if row.pid else None,
            tmux_inventory_complete=inventory.complete,
            tmux_inventory_detail=inventory.issue_detail,
        )
        if row.alive
        else row
        for row in rows
    )
