"""Shared world snapshot — ONE scan per refresh cycle (R11 / D8).

The async refresh used to let every view scan `/proc`, transcripts, and
registries independently. `build_world_snapshot` computes that world ONCE on
the worker thread; `data.refresh.build_refresh_result` derives one complete batch
from it before the App main loop gives that batch to every view.

This is the TOP of the data layer — it composes `sessions` / `membership` /
`liveness`. Only the data layer's top-level `refresh` module imports it, so
there is no cycle. Recoverable external failures are typed by their owning data
module. Expected boundary I/O failures become an explicit `RefreshFailure`;
programming and invariant errors are not converted into empty view data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..models import InventoryIssue, Project, Session
from . import liveness, membership, providers, sessions, tmux
from .sessions import SessionScanResult


@dataclass(frozen=True)
class WorldSnapshot:
    """One cycle's shared view of the machine (read-only data for the views).

    `sessions` is the full transcript-driven scan (SessionsView) and
    `projects` the evidence-tier membership list (ProjectsView).
    `membership_issues` carries the membership sources' (claude.json,
    codex/kimi trust, curation) incompleteness evidence so the Projects status
    can expose a degraded inventory.

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
    projects: tuple[Project, ...] = ()
    # ADR-0007 membership-source degradation (claude.json, codex/kimi trust,
    # curation).
    membership_issues: tuple[InventoryIssue, ...] = ()
    liveness_snapshot: liveness.LivenessSnapshot | None = None
    transcript_scan: SessionScanResult = field(default_factory=SessionScanResult)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "provider_issues", tuple(self.provider_issues))
        object.__setattr__(self, "projects", tuple(self.projects))
        object.__setattr__(
            self,
            "membership_issues",
            tuple(self.membership_issues),
        )


def build_world_snapshot() -> WorldSnapshot:
    """Compute the shared per-cycle world once (worker thread, R11/D8).

    Heavy scans (typed transcript discovery via `sessions.scan_result`) run
    exactly once here instead of once per tab. Session liveness uses targeted
    per-pid `/proc` reads, not another full walk; those inputs are captured
    once and injected into `sessions.scan_result`. Each data owner handles its
    expected external failures.
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
    projects_scan = membership.scan_projects(all_sessions)
    return WorldSnapshot(
        sessions=all_sessions,
        provider_issues=provider_issues,
        projects=projects_scan.projects,
        membership_issues=projects_scan.issues,
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
