"""Execution-time session resolution — fresh evidence for a destructive verb.

Split out of `session_ops.py` for the 600-line budget. A live takeover may
only proceed on a WHOLE Session re-resolved against a fresh generation,
never on snapshot identity: pid, generation, cwd, and current-ness all move
while an operator looks at a list. Claude rows re-resolve through
registry + transcripts here; non-Claude rows re-resolve through their
provider's argv/metadata scan (`providers.resolve_argv_execution`), same
guarantee on different evidence (ADR-0005).

Dead resumes and forks are non-destructive and deliberately skip this path,
so degraded liveness never blocks them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from ..data import liveness, providers, sessions
from ..models import Session, issue_detail


class ExecutionSessionState(StrEnum):
    """Execution-time exact-SID resolution states.

    RESOLVED is discriminated by `.success`; LIVENESS_INCOMPLETE /
    TRANSCRIPT_INCOMPLETE / MISSING are discriminated by
    `do_resume_sid_result` (detail prefixes; MISSING also appends the
    non-Claude provider hint — `--take-over` only ever scans Claude state).
    Every other rejection (ambiguous match, current-session guard, unusable
    cwd, incomplete live identity) collapses into REFUSED, which still
    carries the specific reason in `detail`.
    """

    RESOLVED = "resolved"
    LIVENESS_INCOMPLETE = "liveness_incomplete"
    TRANSCRIPT_INCOMPLETE = "transcript_incomplete"
    MISSING = "missing"
    REFUSED = "refused"


@dataclass(frozen=True)
class ExecutionSessionResolution:
    state: ExecutionSessionState
    session: Session | None = None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is ExecutionSessionState.RESOLVED


def resolve_execution_session(sid: str) -> ExecutionSessionResolution:
    """Resolve one stable SID against one fresh liveness/transcript generation."""
    evidence = liveness.liveness_inputs()
    if not evidence.complete:
        return ExecutionSessionResolution(
            ExecutionSessionState.LIVENESS_INCOMPLETE,
            detail=issue_detail(evidence.issues),
        )
    transcript_scan = sessions.scan_result(evidence)
    if not transcript_scan.complete:
        return ExecutionSessionResolution(
            ExecutionSessionState.TRANSCRIPT_INCOMPLETE,
            detail=issue_detail(transcript_scan.issues),
        )
    matches = tuple(
        session for session in transcript_scan.sessions if session.sid == sid
    )
    if not matches:
        return ExecutionSessionResolution(
            ExecutionSessionState.MISSING,
            detail=f"missing session id {sid!r}",
        )
    if len(matches) != 1:
        return ExecutionSessionResolution(
            ExecutionSessionState.REFUSED,
            detail=f"ambiguous session id {sid!r}; found {len(matches)} exact matches",
        )
    target = matches[0]
    if target.current:
        return ExecutionSessionResolution(
            ExecutionSessionState.REFUSED,
            detail=f"session {sid!r} is the current session",
        )
    if not target.cwd or not os.path.isdir(target.cwd):
        return ExecutionSessionResolution(
            ExecutionSessionState.REFUSED,
            detail=f"session {sid!r} has no usable execution-time cwd: {target.cwd!r}",
        )
    if target.alive:
        missing = []
        if target.pid is None:
            missing.append("pid")
        if not target.proc_start:
            missing.append("proc_start")
        if missing:
            return ExecutionSessionResolution(
                ExecutionSessionState.REFUSED,
                detail=(
                    f"live session {sid!r} has incomplete execution-time identity "
                    f"({', '.join(missing)})"
                ),
            )
    return ExecutionSessionResolution(
        ExecutionSessionState.RESOLVED,
        session=target,
    )


def session_for_execution(
    session: Session,
    fork: bool,
) -> ExecutionSessionResolution:
    base_provider = session.provider.split(":", 1)[0]
    # A Codex row can become app-server-hosted after the rendered generation.
    # Re-resolve every Codex execution (including dead/fork) so stale "dead"
    # evidence can never turn a newly hosted thread into an actionable row.
    needs_provider_refresh = session.alive or session.hosted or base_provider == "codex"
    if not needs_provider_refresh or (fork and base_provider != "codex"):
        return ExecutionSessionResolution(
            ExecutionSessionState.RESOLVED,
            session=session,
        )
    if session.provider != "claude":
        # The Claude resolver below reads registry + transcripts; a live
        # non-Claude takeover re-resolves through its provider's argv scan
        # instead — same guarantee, different evidence: kill only on a fresh
        # whole Session, never on snapshot identity (ADR-0005).
        argv_resolution = providers.resolve_argv_execution(
            session.provider,
            session.sid,
        )
        if not argv_resolution.success:
            return ExecutionSessionResolution(
                ExecutionSessionState.REFUSED,
                detail=argv_resolution.detail,
            )
        if argv_resolution.session is None:
            raise AssertionError("successful provider resolution must carry a Session")
        if argv_resolution.session.hosted:
            return ExecutionSessionResolution(
                ExecutionSessionState.REFUSED,
                detail=f"session {session.sid!r} is app-server hosted and read-only",
            )
        return ExecutionSessionResolution(
            ExecutionSessionState.RESOLVED,
            session=argv_resolution.session,
        )
    return resolve_execution_session(session.sid)
