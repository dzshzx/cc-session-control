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

from dataclasses import dataclass, field

from ..models import AgentJob, EnvRecord, RCProject, RCServer, Session, SessionProc
from . import environments, liveness, proc, rc, registry, sessions


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

    `session_procs` (with `/proc` liveness already injected), `agents_map`
    (`claude agents --json`) and `cur` (the ancestor-pid set) are the raw liveness
    inputs `build_world_snapshot` already computes for the scan; they are exposed
    here so the Sessions cleanup submenu can feed `cleanup_classified` /
    `select_zombie_pids` WITHOUT a second scan (R11/D8).
    """
    sessions: list[Session] = field(default_factory=list)
    agent_jobs: list[AgentJob] = field(default_factory=list)
    rc_projects: list[RCProject] = field(default_factory=list)
    rc_servers: list[RCServer] = field(default_factory=list)
    observed_envs: list[EnvRecord] = field(default_factory=list)
    file_referenced_envs: list[EnvRecord] = field(default_factory=list)
    session_procs: list[SessionProc] = field(default_factory=list)
    agents_map: dict[str, int | None] = field(default_factory=dict)
    cur: set[int] = field(default_factory=set)


def liveness_inputs() -> tuple[
    list[SessionProc], set[int], list[AgentJob], dict[str, int | None]
]:
    """The shared liveness inputs — `(session_procs, cur, agent_jobs,
    agents_map)` — fetched ONCE, jobs already host-enriched.

    Both `build_world_snapshot` and the Sessions view's `fetch_pending(None)`
    self-fetch consume this, so the degraded path is the same assembly instead
    of a hand-kept mirror that can drift. Each read swallows its own errors →
    safe empties.
    """
    session_procs = liveness.live_session_procs()
    try:
        agent_jobs = liveness.enrich_jobs(registry.read_agent_jobs(), session_procs)
    except Exception:
        agent_jobs = []
    try:
        agents_map = liveness.alive_map()
    except Exception:
        agents_map = {}
    return session_procs, proc.ancestor_pids(), agent_jobs, agents_map


def build_world_snapshot() -> WorldSnapshot:
    """Compute the shared per-cycle world once (worker thread, R11/D8).

    Heavy scans (transcript glob via `sessions.scan`, `/proc` walk via
    `rc.scan_servers`) run exactly once here instead of once per tab. The
    registry reads are ~5s-TTL cached so the few repeat reads inside `scan()`
    hit the cache. Each callee swallows its own errors and returns safe empties.
    """
    session_procs, cur, agent_jobs, agents_map = liveness_inputs()
    all_sessions = sessions.scan()
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
        session_procs=session_procs,
        agents_map=agents_map,
        cur=cur,
    )
