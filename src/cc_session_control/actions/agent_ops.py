"""Background-agent lifecycle actions (R4 / Phase 6).

The persistent truth for a background agent lives in `jobs/<short>/state.json`
(registry.read_agent_jobs → AgentJob); it carries NO pid. So a live worker's host
pid is resolved by JOINing the job's sid back to `sessions/<pid>.json`
(`job_host`) — a live worker with no sessions file is therefore unstoppable, a
documented orphan risk surfaced in `HELP`.

Capability red lines honoured here:
  - respawn/takeover never replace the csctl process (respawn spawns a tmux
    window; takeover hands a Session to the existing `do_resume` path run AFTER
    the UI loop exits).
  - stop only signals a confirmed-live joined host pid; killing a
    `--remote-control`/bg worker does not always fully reap it (orphan risk).
  - destructive ops (remove/stop) refuse when "current" can't be determined
    (no `/proc`, R10) so they never blind-hit csctl's own session.

This is an action module: internals are English, but the user-facing label/help
constants the (Phase 7) background view reads are Simplified Chinese.
"""

from __future__ import annotations

import os
import shlex

from ..config import cfg
from ..data import cleanup, liveness, proc, registry, tmux
from ..data.removal import CleanupExecution
from ..models import AgentJob, Session
from . import session_ops

# --- host-pid join (shared by stop_job, remove_job, and the view) -------------

def job_host(
    job: AgentJob, *, max_age: float = 5.0
) -> tuple[int | None, bool]:
    """Resolve a background job's host pid + liveness — `(pid, alive)`.

    `state.json` has no pid, so the worker's pid is JOINed from
    `sessions/<pid>.json` on `job.sid` (a bg session proc; `kind` is typically
    "bg"). Prefers a `/proc`-confirmed live match (so `alive=True` is trustworthy
    and defeats pid reuse via `procStart`); falls back to the first sid match
    with `alive=False`. Returns `(None, False)` when no sessions file exists for
    the sid — that live worker is unstoppable (documented orphan risk).

    Injects `/proc` liveness onto the registry rows, then defers to the single
    pure join `registry.host_pid_for_sid` (shared with `snapshot._enrich_jobs`).
    """
    procs = liveness.live_session_procs(max_age=max_age)
    return registry.host_pid_for_sid(job.sid, procs)


# --- respawn ------------------------------------------------------------------

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


def respawn(job: AgentJob) -> str:
    """Relaunch a background agent in tmux; returns the exact command string.

    Runs `respawn_cmd(job)` in the job's per-project tmux session
    (`tmux.session_name_for(job.cwd)`) so it outlives the terminal — it does NOT
    os.exec/replace the csctl process. The returned string also feeds the
    clipboard `y`-style key.
    """
    cmd = respawn_cmd(job)
    tmux.run_in_tmux(tmux.session_name_for(job.cwd), _job_window(job), cmd)
    return cmd


# --- remove (settled agents only) ---------------------------------------------

def remove_job(job: AgentJob) -> CleanupExecution:
    """Remove a SETTLED background agent: `jobs/<short>/` + its sid artifacts.

    Refuses for a LIVE worker (`job_host` reports alive) and when "current"
    can't be determined (no `/proc`, R10) — destructive, must not run blind.
    """
    result = CleanupExecution()
    if not proc.current_determinable():
        result.refuse([job.short], "current session cannot be determined")
        return result
    try:
        _, alive = job_host(job, max_age=0.0)
    except OSError as exc:
        result.refuse([job.short], f"liveness revalidation failed: {exc}")
        return result
    if alive:
        result.skip(job.short, "background agent is now live")
        return result
    return cleanup.remove_agent_artifacts(job.short, job.sid)


# --- watch (read-only) --------------------------------------------------------

def watch(job: AgentJob) -> str | None:
    """Path to the job's read-only `jobs/<short>/timeline.jsonl`, or None.

    Pure lookup, no mutation — returns the path only when the file exists so the
    view can fall back gracefully (R4.4 read-only watch).
    """
    path = os.path.join(str(cfg.jobs_dir), job.short, "timeline.jsonl")
    return path if os.path.isfile(path) else None


# --- resume takeover (reuses the existing foreground resume path) -------------

def resume_takeover(job: AgentJob) -> Session:
    """Adapt a background job into a `Session` for the EXISTING resume path.

    Bringing a bg session to the foreground is just a resume of its
    `resume_sid`, so this returns a Session the view feeds to the SAME
    `app.exit_with(ResumeIntent)` → `do_resume` pipeline used for foreground sessions —
    all kill/exec/`_resume_plan` logic is reused, none duplicated (R4.4 takeover).
    `pid`/`alive` come from the host join so a live worker is killed first
    (resume = takeover); `current` is computed so the launching session stays
    protected. `tmux_target` is filled here at action time (the agents list
    renders no ⧉ badge, so there is no batch snapshot value to reuse) so the
    tmux-first Enter can enter a resident worker in place. Does NOT itself
    replace the csctl process.
    """
    pid, alive = job_host(job)
    current = bool(pid) and pid in proc.ancestor_pids()
    return Session(
        sid=job.resume_sid,
        cwd=job.cwd,
        label=job.name or job.short,
        mtime=0.0,
        prompts=0,
        pid=pid,
        alive=alive,
        current=current,
        proc_start=_host_start(pid),
        source="bg",
        agent_short=job.short,
        tmux_target=tmux.find_session_window([pid]) if alive and pid else None,
    )


def _host_start(pid: int | None) -> str:
    """`proc_start` of the joined host pid ("" when unknown).

    `job_host` deliberately keeps its small `(pid, alive)` shape; this second
    lookup feeds `take_over`'s kill-time `pid_alive` recheck so the pid-reuse
    window is closed on the bg-agent path too, not just for scanned sessions.
    """
    if not pid:
        return ""
    return next(
        (sp.proc_start for sp in liveness.live_session_procs() if sp.pid == pid),
        "",
    )


# --- stop (live workers only) -------------------------------------------------

def stop_job(job: AgentJob) -> bool:
    """Stop a LIVE background worker via its joined host pid. True iff signalled.

    The host pid is JOINed from `sessions/<pid>.json` (`job_host`); only a
    confirmed-live pid is killed — a worker with no sessions file is unstoppable
    (no-op False, orphan risk). The kill itself is `session_ops.take_over` (the
    ONE primitive: R10 gate, recheck, SIGTERM, cache invalidation); the early
    R10 check here just skips the join IO when the answer is already no.
    Killing does not always fully reap a `--remote-control`/bg worker (orphan
    risk, see `HELP`).
    """
    if not proc.current_determinable():
        return False
    pid, alive = job_host(job)
    if not alive or not pid:
        return False
    return session_ops.take_over(pid, _host_start(pid)) in session_ops.TAKE_OVER_OK
