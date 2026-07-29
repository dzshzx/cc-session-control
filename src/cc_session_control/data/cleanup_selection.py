"""Pure cleanup candidate selection from one captured generation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet

from ..models import AgentJob, Session, SessionProc


def known_sids(
    sessions: Sequence[Session],
    session_procs: Sequence[SessionProc],
    agent_jobs: Sequence[AgentJob],
    agents_map: Mapping[str, int | None],
    cur: AbstractSet[int],
) -> set[str]:
    """Return the union of transcript, registry, live, and current sids."""
    return known_sids_from_transcripts(
        (session.sid for session in sessions),
        session_procs,
        agent_jobs,
        agents_map,
        cur,
    )


def known_sids_from_transcripts(
    transcript_sids: Iterable[str],
    session_procs: Sequence[SessionProc],
    agent_jobs: Sequence[AgentJob],
    agents_map: Mapping[str, int | None],
    cur: AbstractSet[int],
) -> set[str]:
    """Return every sid protected by transcripts or liveness evidence."""
    known = set(transcript_sids)
    known |= {sp.sid for sp in session_procs}
    known |= {sp.sid for sp in session_procs if sp.proc_alive}
    known |= {sp.sid for sp in session_procs if sp.pid in cur}
    for job in agent_jobs:
        if job.sid:
            known.add(job.sid)
        if job.resume_sid:
            known.add(job.resume_sid)
        if job.host_alive and job.sid:
            known.add(job.sid)
    known |= {sid for sid in agents_map if sid}
    return known


def select_prunable_sessions(
    sessions: Sequence[Session],
    max_prompts: int,
    now: float,
) -> list[Session]:
    """Select old, non-live sessions without acquiring protection evidence."""
    alive_sids = {session.sid for session in sessions if session.alive}
    return [
        session
        for session in sessions
        if session.prompts <= max_prompts
        and session.sid not in alive_sids
        and not session.current
        and (now - session.mtime) > 600
    ]
