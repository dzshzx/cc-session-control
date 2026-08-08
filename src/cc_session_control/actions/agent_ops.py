"""Background-agent lifecycle actions (R4 / Phase 6).

The persistent truth for a background agent lives in `jobs/<short>/state.json`
(registry.read_agent_jobs → AgentJob); it carries NO pid. So a live worker's host
pid is resolved by JOINing the job's sid back to `sessions/<pid>.json`
(the production takeover path does this inside one typed liveness snapshot via
`registry.host_pid_for_sid`) — a live worker with no sessions file is therefore
unstoppable, a documented orphan risk surfaced in `HELP`.

Capability red lines honoured here:
  - respawn/takeover never replace the csctl process (respawn spawns a tmux
    window; takeover hands a Session to the existing `do_resume_result` path run
    AFTER the UI loop exits).
  - stop only signals a confirmed-live joined host pid; killing a
    `--remote-control`/bg worker does not always fully reap it (orphan risk).
  - destructive ops (remove/stop) refuse when "current" can't be determined
    (no `/proc`, R10) so they never blind-hit csctl's own session.

This is an action module: internals are English, but the user-facing label/help
constants the (Phase 7) background view reads are Simplified Chinese.
"""

from __future__ import annotations

import shlex
from collections import deque
from dataclasses import dataclass
from enum import Enum

from ..config import cfg
from ..data import cleanup, liveness, registry, tmux
from ..data import proc as proc
from ..data.removal import CleanupExecution
from ..models import AgentJob, Session, issue_detail
from . import session_ops

# --- respawn ------------------------------------------------------------------


@dataclass(frozen=True)
class RespawnResult:
    """Exact command plus the tmux target that proves it was launched."""

    command: str
    target: str | None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.target is not None


def respawn_cmd(job: AgentJob) -> str:
    """The exact relaunch command: `claude --resume <resume_sid> <flags> --bg`.

    Pure string build via `shlex.join` (split from `respawn` so it can be copied
    to the clipboard / asserted in tests). `respawn_flags` are reused verbatim
    from the recorded job state.
    """
    args = ["claude", "--resume", job.resume_sid, *job.respawn_flags, "--bg"]
    return shlex.join(args)


def _job_window(job: AgentJob) -> str:
    """tmux window name for a respawned agent (name or short, suffixed)."""
    base = (job.name or "bg").strip() or "bg"
    return f"{base}-{job.short[:8]}"


def respawn_result(job: AgentJob) -> RespawnResult:
    """Relaunch a background agent from its project cwd, retaining outcome."""
    cmd = respawn_cmd(job)
    tmux_cmd = f"cd {shlex.quote(job.cwd)} && {cmd}" if job.cwd else cmd
    result = tmux.run_in_tmux_result(
        cfg.tmux_session,
        tmux.window_name_for(job.cwd, _job_window(job)),
        tmux_cmd,
    )
    target = result.target if result.success else None
    return RespawnResult(cmd, target, result.diagnostic)


# --- remove (settled agents only) ---------------------------------------------


def remove_job(job: AgentJob) -> CleanupExecution:
    """Remove a SETTLED background agent: `jobs/<short>/` + its sid artifacts.

    The public data-layer executor owns the final fresh liveness check, so no
    caller-held evidence can authorize deletion.
    """
    result = CleanupExecution()
    try:
        anchors = cleanup.agent_removal_anchors(job.short, job.sid)
    except OSError as exc:
        result.refuse([job.short], f"cannot establish removal anchor: {exc}")
        return result
    return cleanup.remove_agent_artifacts(job.short, job.sid, anchors=anchors)


# --- watch (read-only) --------------------------------------------------------

_TIMELINE_LINE_LIMIT = 200


class TimelineReadState(Enum):
    READY = "ready"
    MISSING = "missing"
    FAILED = "failed"


@dataclass(frozen=True)
class TimelineReadResult:
    """Bounded timeline data for a main-loop read-only overlay."""

    state: TimelineReadState
    lines: tuple[str, ...] = ()
    detail: str = ""


def watch(job: AgentJob) -> TimelineReadResult:
    """Stream the last 200 timeline lines without loading the whole file."""
    path = cfg.jobs_dir / job.short / "timeline.jsonl"
    try:
        with open(path, errors="ignore") as fh:
            lines = deque(
                (line.rstrip("\r\n") for line in fh),
                maxlen=_TIMELINE_LINE_LIMIT,
            )
    except FileNotFoundError:
        return TimelineReadResult(TimelineReadState.MISSING)
    except OSError as exc:
        return TimelineReadResult(TimelineReadState.FAILED, detail=str(exc))
    return TimelineReadResult(TimelineReadState.READY, tuple(lines))


# --- resume takeover (reuses the existing foreground resume path) -------------


class TakeoverPreparationState(Enum):
    """Whether one fresh generation permits constructing a takeover session."""

    READY = "ready"
    REFUSED = "refused"


@dataclass(frozen=True)
class TakeoverPreparationResult:
    """Typed result of preparing one background-agent takeover."""

    state: TakeoverPreparationState
    session: Session | None = None
    detail: str = ""


def _ready_takeover(
    job: AgentJob,
    *,
    pid: int | None = None,
    alive: bool = False,
    current: bool = False,
    proc_start: str = "",
    tmux_target: str | None = None,
) -> TakeoverPreparationResult:
    """Build the existing resume-path adapter for one prepared agent."""
    return TakeoverPreparationResult(
        TakeoverPreparationState.READY,
        session=Session(
            sid=job.resume_sid,
            cwd=job.cwd,
            label=job.name or job.short,
            mtime=0.0,
            prompts=0,
            pid=pid,
            alive=alive,
            current=current,
            proc_start=proc_start,
            source="bg",
            agent_short=job.short,
            tmux_target=tmux_target,
        ),
    )


def prepare_takeover(job: AgentJob) -> TakeoverPreparationResult:
    """Prepare one background-agent resume without widening destructive gates.

    Bringing a bg session to the foreground is just a resume of its
    `resume_sid`, so a ready result carries the Session the view feeds to the SAME
    `app.exit_with(ResumeIntent)` → `do_resume_result` pipeline used for foreground
    sessions — all kill/exec/`_resume_plan` logic is reused, none duplicated
    (R4.4 takeover).

    The published immutable ``job.host_alive`` decides whether this action can
    kill. A dead job resumes directly without acquiring liveness or tmux state.
    A live job acquires exactly one fresh typed generation; incomplete registry,
    process, ancestor, or agents evidence is refused before any tmux lookup.
    Host identity, liveness, proc-start, and current-session protection all come
    from that same generation. If it proves the former host is gone, the action
    safely contracts to the same non-destructive dead resume.
    """
    if not job.host_alive:
        return _ready_takeover(job)

    evidence = liveness.liveness_inputs()
    if not evidence.complete:
        return TakeoverPreparationResult(
            TakeoverPreparationState.REFUSED,
            detail="liveness evidence incomplete: " + issue_detail(evidence.issues),
        )
    pid, alive = registry.host_pid_for_sid(job.sid, evidence.session_procs)
    if not alive or pid is None:
        return _ready_takeover(job)

    proc_start = next(
        (
            item.proc_start
            for item in evidence.session_procs
            if item.sid == job.sid and item.pid == pid and item.proc_alive is True
        ),
        "",
    )
    residency = tmux.find_session_window_result([pid])
    if not residency.complete:
        detail = "; ".join(
            f"{issue.source}"
            + (f" at {issue.path}" if issue.path else "")
            + f": {issue.detail}"
            for issue in residency.issues
        )
        return TakeoverPreparationResult(
            TakeoverPreparationState.REFUSED,
            detail=detail,
        )
    return _ready_takeover(
        job,
        pid=pid,
        alive=True,
        current=pid in evidence.cur,
        proc_start=proc_start,
        tmux_target=residency.target,
    )


# --- stop (live workers only) -------------------------------------------------


class AgentStopState(Enum):
    """Observable outcome of stopping one background agent."""

    STOPPED = "stopped"
    NOT_RUNNING = "not-running"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class AgentStopResult:
    state: AgentStopState
    pid: int | None = None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is AgentStopState.STOPPED


def stop_job_result(job: AgentJob) -> AgentStopResult:
    """Stop one live background agent from one fresh liveness snapshot.

    The host pid is JOINed from `sessions/<pid>.json` (`registry.host_pid_for_sid`);
    only a confirmed-live pid is killed. Incomplete source evidence and unavailable
    current-session determination refuse the destructive action. The same
    snapshot supplies both the host pid and its proc-start identity, avoiding
    a second compatibility scan before ``take_over_result`` rechecks liveness.

    The kill itself is `session_ops.take_over_result` (the ONE primitive: R10 gate,
    recheck, SIGTERM, cache invalidation). Its four outcomes map one-for-one to
    this domain result.

    Killing does not always fully reap a `--remote-control`/bg worker (orphan
    risk, see `HELP`).
    """
    evidence = liveness.liveness_inputs()
    if not evidence.complete:
        return AgentStopResult(
            AgentStopState.REFUSED,
            detail="liveness evidence incomplete: " + issue_detail(evidence.issues),
        )
    pid, alive = registry.host_pid_for_sid(job.sid, evidence.session_procs)
    if not alive or not pid:
        return AgentStopResult(
            AgentStopState.NOT_RUNNING,
            pid=pid,
            detail="no live host for background agent",
        )
    proc_start = next(
        (
            item.proc_start
            for item in evidence.session_procs
            if item.sid == job.sid and item.pid == pid
        ),
        "",
    )
    outcome = session_ops.take_over_result(pid, proc_start)
    if outcome.state is session_ops.TakeOverState.KILLED:
        return AgentStopResult(AgentStopState.STOPPED, pid=pid)
    if outcome.state is session_ops.TakeOverState.GONE:
        return AgentStopResult(
            AgentStopState.NOT_RUNNING,
            pid=pid,
            detail="background agent host is no longer running",
        )
    if outcome.state is session_ops.TakeOverState.REFUSED:
        return AgentStopResult(
            AgentStopState.REFUSED,
            pid=pid,
            detail=outcome.detail or "current session cannot be determined",
        )
    return AgentStopResult(
        AgentStopState.FAILED,
        pid=pid,
        detail=outcome.detail or "failed to signal background agent host",
    )
