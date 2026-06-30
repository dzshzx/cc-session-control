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
import signal
import time
from dataclasses import replace

from ..config import cfg
from ..data import cleanup, liveness, proc, rc, registry
from ..models import AgentJob, Session

# --- User-facing labels/help (Simplified Chinese, read by the Phase 7 view) ---

TAKEOVER_LABEL = "接回"

# Unified verb table (matches Sessions/RC): Enter/o=接回(primary), s=停止(kill a
# live thing), d=删除, R=重启(respawn). `r`=刷新 lives in the App-level footer
# prefix now, so it is NOT repeated here; separators are ` · ` like the other tabs.
KEYHINTS = "Enter/o 接回 · s 停止 · d 删除 · w 查看 · R 重启"

# Orphan-process risk (R4.5 red line): stop only kills the host pid joined from
# the sessions registry, killing a --remote-control/bg worker does not always
# fully reap it, and a live worker with no sessions file can't be located at all.
HELP = (
    "后台 agent 生命周期：\n"
    "  Enter/o 接回(拉回前台，复用 resume；接运行中的 agent 会先确认接管)  w 查看 timeline(只读)\n"
    "  R 重启(respawn)  d 删除(仅已结束)  s 停止(仅运行中，需确认)  r 刷新\n"
    "停止/孤儿风险：停止只能杀经 sessions 文件 join 到的 host pid；"
    "杀 --remote-control/后台 worker 不一定彻底回收，可能残留孤儿进程，需手动确认；"
    "找不到运行中 worker 的 host pid 时无法停止。"
)


# --- host-pid join (shared by stop_job, remove_job, and the view) -------------

def job_host(job: AgentJob) -> tuple[int | None, bool]:
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
    procs = [
        replace(sp, proc_alive=proc.pid_alive(sp.pid, sp.proc_start))
        for sp in registry.read_session_procs()
    ]
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

    Runs `respawn_cmd(job)` in the shared tmux session (`cfg.tmux_session`) so it
    outlives the terminal — it does NOT os.exec/replace the csctl process. The
    returned string also feeds the clipboard `y`-style key.
    """
    cmd = respawn_cmd(job)
    rc.run_in_tmux(cfg.tmux_session, _job_window(job), cmd)
    return cmd


# --- remove (settled agents only) ---------------------------------------------

def remove_job(job: AgentJob) -> bool:
    """Remove a SETTLED background agent: `jobs/<short>/` + its sid artifacts.

    Returns True iff the job dir was removed. Refuses (False) for a LIVE worker
    (`job_host` reports alive) and when "current" can't be determined (no
    `/proc`, R10) — destructive, must not run blind.
    """
    if not proc.current_determinable():
        return False
    _, alive = job_host(job)
    if alive:
        return False
    job_dir = os.path.join(str(cfg.jobs_dir), job.short)
    removed = cleanup._remove_path(job_dir)
    # Reuse cleanup's artifact-path helper / remover: it returns the sid-keyed
    # dirs (session-env/file-history/tasks/uploads) plus jobs/<sid[:8]> (which
    # usually equals job_dir — a second remove is a harmless no-op), so the job's
    # session leaves no orphan artifacts behind.
    for path in cleanup._session_artifact_paths(job.sid):
        cleanup._remove_path(path)
    return removed


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
    `app.exit_with_resume` → `do_resume` pipeline used for foreground sessions —
    all kill/exec/`_resume_plan` logic is reused, none duplicated (R4.4 takeover).
    `pid`/`alive` come from the host join so a live worker is killed first
    (resume = takeover); `current` is computed so the launching session stays
    protected. Does NOT itself replace the csctl process.
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
        source="bg",
        agent_short=job.short,
    )


# --- stop (live workers only) -------------------------------------------------

def stop_job(job: AgentJob) -> bool:
    """Stop a LIVE background worker via its joined host pid. True iff signalled.

    The host pid is JOINed from `sessions/<pid>.json` (`job_host`); only a
    confirmed-live pid is killed — a worker with no sessions file is unstoppable
    (no-op False, orphan risk). Refuses when "current" can't be determined (no
    `/proc`, R10). Owns the liveness-cache invalidation (like terminate). Killing
    does not always fully reap a `--remote-control`/bg worker (orphan risk, see
    `HELP`).
    """
    if not proc.current_determinable():
        return False
    pid, alive = job_host(job)
    if not alive or not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        liveness.invalidate_cache()  # already gone — liveness changed
        return True
    except Exception:
        return False
    time.sleep(1)
    liveness.invalidate_cache()
    return True
