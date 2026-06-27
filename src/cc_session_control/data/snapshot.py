"""Shared world snapshot — ONE scan per refresh cycle (R11 / D8).

The async refresh used to call `fetch_pending()` on every view, so three tabs
each re-scanned `/proc`, the transcripts, and the registries. `build_world_snapshot`
computes that world ONCE on the worker thread; `App` then hands the same
immutable snapshot to each view's `fetch_pending(snapshot)` so they only project
it (no per-view IO). Views stay back-compatible: `fetch_pending(None)` self-fetches.

This is the TOP of the data layer — it composes `sessions` / `rc` / `registry` /
`environments` / `proc`. Nothing in `data/` imports it (only `app`/`views` do),
so there is no cycle. Errors are swallowed by the callees; `App` additionally
guards `build_world_snapshot` so a failed build degrades to per-view self-fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..models import AgentJob, EnvRecord, RCProject, RCServer, Session, SessionProc
from . import environments, proc, rc, registry, sessions


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
    """
    sessions: list[Session] = field(default_factory=list)
    agent_jobs: list[AgentJob] = field(default_factory=list)
    rc_projects: list[RCProject] = field(default_factory=list)
    rc_servers: list[RCServer] = field(default_factory=list)
    observed_envs: list[EnvRecord] = field(default_factory=list)
    file_referenced_envs: list[EnvRecord] = field(default_factory=list)


def _enrich_jobs(
    jobs: list[AgentJob], session_procs: list[SessionProc]
) -> list[AgentJob]:
    """Fill each job's `host_pid`/`host_alive` by joining sid -> sessions/<pid>.

    `state.json` carries no pid, so a live worker's host pid is the proc-alive
    `sessions/<pid>.json` for the job's sid (falling back to the first match when
    none is alive). Returns fresh copies so the cached registry objects are never
    mutated. Mirrors `agent_ops.job_host` but stays in the data layer (snapshot
    must not import actions).
    """
    by_sid: dict[str, list[SessionProc]] = {}
    for sp in session_procs:
        by_sid.setdefault(sp.sid, []).append(sp)
    out: list[AgentJob] = []
    for job in jobs:
        procs = by_sid.get(job.sid, [])
        pid: int | None = None
        alive = False
        for sp in procs:
            if sp.proc_alive:
                pid, alive = sp.pid, True
                break
        if pid is None and procs:
            pid = procs[0].pid
        out.append(replace(job, host_pid=pid, host_alive=alive))
    return out


def build_world_snapshot() -> WorldSnapshot:
    """Compute the shared per-cycle world once (worker thread, R11/D8).

    Heavy scans (transcript glob via `sessions.scan`, `/proc` walk via
    `rc.scan_servers`) run exactly once here instead of once per tab. The
    registry reads are ~5s-TTL cached so the few repeat reads inside `scan()`
    hit the cache. Each callee swallows its own errors and returns safe empties.
    """
    session_procs = [
        replace(sp, proc_alive=proc.pid_alive(sp.pid, sp.proc_start))
        for sp in registry.read_session_procs()
    ]
    all_sessions = sessions.scan()
    agent_jobs = _enrich_jobs(registry.read_agent_jobs(), session_procs)
    rc_projects = rc.scan()
    rc_servers = rc.scan_servers()
    # R6 ledger persistence (the whole point of the ledger): record EVERY env an
    # on-disk file references THIS cycle — session_* + cse_* + the env_* captured
    # from rc servers — using the bridge-truthy (NOT alive-gated) set for
    # membership. When one of these later toggles away (RC turned off / job
    # removed / server stopped) it stays in the ledger but drops out of the
    # file-referenced set, surfacing as an orphan / manual-delete candidate. Cheap
    # and safe on the worker thread: the ledger is write-on-change + flock +
    # compacted, so re-observing the same set is a no-op rewrite.
    file_referenced_envs = environments.observe(session_procs, agent_jobs, rc_servers)
    environments.upsert(file_referenced_envs)
    # CURRENT must be alive-gated (R3/R6): pass the already-liveness-resolved
    # session_procs + host-enriched agent_jobs + running servers so a zombie's
    # stale bridge is NOT counted as a bound (current) environment.
    observed_envs = environments.observe_live(session_procs, agent_jobs, rc_servers)
    return WorldSnapshot(
        sessions=all_sessions,
        agent_jobs=agent_jobs,
        rc_projects=rc_projects,
        rc_servers=rc_servers,
        observed_envs=observed_envs,
        file_referenced_envs=file_referenced_envs,
    )
