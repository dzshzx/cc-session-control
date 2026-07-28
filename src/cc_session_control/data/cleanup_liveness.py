"""Liveness assembly used by cleanup preview and destructive revalidation."""

from __future__ import annotations

from ..models import AgentJob, SessionProc
from . import liveness, proc, registry


def fill_liveness_inputs(
    session_procs: list[SessionProc] | None,
    agent_jobs: list[AgentJob] | None,
    agents_map: dict[str, int | None] | None,
    cur: set[int] | None,
    *,
    fresh: bool = False,
) -> tuple[list[SessionProc], list[AgentJob], dict[str, int | None], set[int]]:
    """Fill omitted inputs, bypassing caches for confirmed execution."""
    if (
        session_procs is None
        or agent_jobs is None
        or agents_map is None
        or cur is None
    ):
        if fresh:
            defaults = fresh_liveness_inputs()
        else:
            d_procs, d_cur, d_jobs, d_agents = liveness.liveness_inputs()
            defaults = d_procs, d_jobs, d_agents, d_cur
        d_procs, d_jobs, d_agents, d_cur = defaults
        session_procs = d_procs if session_procs is None else session_procs
        agent_jobs = d_jobs if agent_jobs is None else agent_jobs
        agents_map = d_agents if agents_map is None else agents_map
        cur = d_cur if cur is None else cur
    return session_procs, agent_jobs, agents_map, cur


def fresh_liveness_inputs() -> tuple[
    list[SessionProc], list[AgentJob], dict[str, int | None], set[int]
]:
    """Read every cleanup protection source with its cache disabled."""
    session_procs = liveness.live_session_procs(max_age=0.0)
    jobs = liveness.enrich_jobs(
        registry.read_agent_jobs(max_age=0.0), session_procs
    )
    return (
        session_procs,
        jobs,
        liveness.alive_map(max_age=0.0),
        proc.ancestor_pids(),
    )


def fresh_session_guards() -> tuple[
    list[SessionProc], dict[str, int | None], set[int]
]:
    """Read only the protection sources needed for session deletion."""
    return (
        liveness.live_session_procs(max_age=0.0),
        liveness.alive_map(max_age=0.0),
        proc.ancestor_pids(),
    )
