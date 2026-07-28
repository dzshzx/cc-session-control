"""Cleanup strategies for Claude Code's on-disk state (D6/R7).

Two strategies, both preview-first: `build_plan` freezes the candidate lists
once per cycle (`CleanupPlan` — the ONE source for the classified counts, the
preview overlay, and the CLI dry-run), and the matching `execute_*` write
deletes AT MOST that frozen list, revalidating every item against fresh
protection data at execution time (删除 ⊆ 预览 — 宁可少删):

- **Strategy A — key-typed orphan sweep.** Key semantics are PER DIRECTORY,
  never a blanket `uuid == sessionId` rule:
    * sid-keyed dirs (`session-env`, `file-history`, `tasks`, `uploads`):
      orphan = an entry whose name (a sessionId) is not in the PROTECTED sid set.
      That set (H1 safety, `known_sids`) is the union of transcript sids,
      registry `sessions/<pid>.json` + `jobs/<short>/state.json` sids, live sids
      (`claude agents --json`, proc-alive, host-alive jobs), and the current
      session — so the sweep never deletes artifacts of a registry-known, live,
      or current session/agent even when its transcript was dropped.
    * pid-keyed dir (`sessions/<pid>.json`): remove only zombies
      (`not pid_alive`), excluding the current session's pid AND any live pid —
      for a resumed multi-pid sid we drop the dead pid files but keep the alive
      one.
    * `debug/`: its uuids are debug-run ids, NOT sessionIds — never treated as
      sid-orphans (it is simply not in the sid-keyed set).
- **Strategy B — age sweep** for non-session-keyed global dirs
  (`shell-snapshots`, `telemetry`, `plans`, `backups`, `paste-cache`): remove
  entries with an mtime older than `cfg.cleanup_age_days`.

`jobs/` is deliberately NOT auto-orphan-swept (only Phase 6's explicit per-job
remove touches it). All paths come from `cfg.*` props — no inline path joins.

R10 safety: when the "current" session can't be determined (no `/proc`),
destructive execution returns a typed refusal rather than failing open. Strategy
B is mtime-only and session-agnostic, so it is not gated on `/proc`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ..config import cfg
from ..models import AgentJob, Session, SessionProc
from . import liveness, proc, registry
from .cleanup_liveness import (
    fill_liveness_inputs,
    fresh_session_guards,
)
from .removal import (
    CleanupExecution,
    CleanupIssue,
    CleanupPlan,
    PathRemoval,
    RemovalStatus,
    remove_path as _remove_path,
)

# Dirs keyed by full sessionId — orphan = name not in the known sid set.
_SID_DIRS = ("session_env", "file_history", "tasks", "uploads")
# Dirs swept purely by mtime (not session-keyed).
_AGE_DIRS = ("shell_snapshots", "telemetry", "plans", "backups", "paste_cache")

_SECONDS_PER_DAY = 86400


def _is_child_name(name: str) -> bool:
    return name not in ("", ".", "..") and os.sep not in name


def _sid_dir_paths() -> list[tuple[str, str]]:
    """(label, path) for each sid-keyed directory, via cfg props only."""
    return [(name.replace("_", "-"), str(getattr(cfg, f"{name}_dir")))
            for name in _SID_DIRS]


def _age_dir_paths() -> list[tuple[str, str]]:
    """(label, path) for each age-swept directory, via cfg props only."""
    return [(name.replace("_", "-"), str(getattr(cfg, f"{name}_dir")))
            for name in _AGE_DIRS]


def _sid_keyed_paths(sid: str) -> list[str]:
    """The sid-keyed artifact dirs (session-env/file-history/tasks/uploads)."""
    return [os.path.join(p, sid) for _, p in _sid_dir_paths()]


def _jobs_path(sid: str) -> str:
    """The 8-char-prefixed `jobs/<short>` dir for a session id."""
    return os.path.join(str(cfg.jobs_dir), sid[:8])


def _session_artifact_paths(sid: str) -> list[str]:
    """All on-disk artifact paths owned by one session id (cfg-derived).

    Covers the sid-keyed dirs plus the 8-char-prefixed `jobs/<short>` dir for
    this session. Used by `remove_agent_artifacts` (whose caller,
    `agent_ops.remove_job`, has already alive-gated the job). `remove_session`
    does NOT use this — it guards the `jobs/<short>` path separately so a LIVE
    agent worker's jobs dir is never deleted (M3).
    """
    return _sid_keyed_paths(sid) + [_jobs_path(sid)]


def remove_agent_artifacts(short: str, sid: str) -> CleanupExecution:
    """Delete a settled background agent's `jobs/<short>` dir + sid artifacts.

    Removes `jobs/<short>` plus every sid-keyed artifact path. The typed result
    retains every filesystem outcome; duplicate job paths are attempted once.

    The CALLER owns the gates: this function assumes the job has already been
    alive-gated (a LIVE worker must never reach here) and that
    `proc.current_determinable()` (R10) has already been checked — it only
    deletes.
    """
    job_dir = os.path.join(str(cfg.jobs_dir), short)
    result = CleanupExecution()
    paths = dict.fromkeys([job_dir, *_session_artifact_paths(sid)])
    for path in paths:
        result.add_removal(_remove_path(path))
    if result.removed and not result.failed:
        result.complete(short)
    elif not result.removed and not result.failed:
        result.mark_missing(short)
    return result


# --- Strategy A: sid-keyed orphan dirs (H1 protected-sid set) --------------

def known_sids(
    sessions: list[Session],
    session_procs: list[SessionProc],
    agent_jobs: list[AgentJob],
    agents_map: dict[str, int | None],
    cur: set[int],
) -> set[str]:
    """Sids whose sid-keyed artifacts must NOT be swept (H1 safety) — PURE.

    A sid-keyed dir is an orphan only when its sid is in NONE of these protected
    sets, so the sweep never deletes artifacts of a registry-known, live, or
    current session/agent (the old `{s.sid for s in sessions}` dropped no-cwd
    bg/bridge stubs and ignored the registry + liveness entirely):
      - transcript scan (`sessions`), incl. the current one
      - registry `sessions/<pid>.json` sids (`session_procs`)
      - registry `jobs/<short>/state.json` sids + resume sids (`agent_jobs`)
      - live per `claude agents --json` (`agents_map`)
      - proc-alive in `session_procs` (defeats pid reuse)
      - host-alive agent jobs
      - the current (csctl-launching) session (`s.current` / pid in `cur`)
    Inputs injected so it stays unit-testable.
    """
    known: set[str] = {s.sid for s in sessions}
    known |= {s.sid for s in sessions if s.current}
    known |= {sp.sid for sp in session_procs}
    known |= {sp.sid for sp in session_procs if sp.proc_alive}
    known |= {sp.sid for sp in session_procs if sp.pid in cur}
    for j in agent_jobs:
        if j.sid:
            known.add(j.sid)
        if j.resume_sid:
            known.add(j.resume_sid)
        if j.host_alive and j.sid:
            known.add(j.sid)
    known |= {sid for sid in agents_map if sid}
    return known


def _gather_known(
    sessions: list[Session],
    session_procs: list[SessionProc] | None = None,
    agent_jobs: list[AgentJob] | None = None,
    agents_map: dict[str, int | None] | None = None,
    cur: set[int] | None = None,
    *,
    fresh: bool = False,
) -> set[str]:
    """Resolve protected sids, self-fetching omitted liveness inputs."""
    session_procs, agent_jobs, agents_map, cur = fill_liveness_inputs(
        session_procs, agent_jobs, agents_map, cur, fresh=fresh)
    return known_sids(sessions, session_procs, agent_jobs, agents_map, cur)


def list_orphan_dirs(
    sessions: list[Session],
    *,
    session_procs: list[SessionProc] | None = None,
    agent_jobs: list[AgentJob] | None = None,
    agents_map: dict[str, int | None] | None = None,
    cur: set[int] | None = None,
) -> list[str]:
    """Orphan sid-keyed artifact entries (`<dir>/<sid>`), preview list.

    An entry is an orphan only when its sid is NOT in the protected set (H1).
    Refuses (returns []) when current can't be determined (R10).
    """
    if not proc.current_determinable():
        return []
    known = _gather_known(sessions, session_procs, agent_jobs, agents_map, cur)
    orphans: list[str] = []
    for label, path in _sid_dir_paths():
        try:
            names = os.listdir(path)
        except FileNotFoundError:
            continue
        for name in names:
            if name not in known:
                orphans.append(os.path.join(label, name))
    return sorted(set(orphans))


def execute_orphan_removals(
    entries: list[str],
    *,
    sessions: list[Session] | None = None,
    known: set[str] | None = None,
) -> CleanupExecution:
    """Delete AT MOST the previewed orphan entries (`<label>/<sid>`).

    删除 ⊆ 预览 + revalidation: only entries from the frozen preview list are
    touched, and each sid is re-checked against a FRESH protection set —
    `known_sids` over `sessions` (pass a freshly scanned transcript list;
    scanning here would invert the data DAG) plus the self-fetched registry /
    liveness / current sources — so a sid that became known between preview
    and confirm is skipped, never the other way around. `known` overrides the
    assembly entirely (tests). Refuses without `/proc` (R10).
    """
    result = CleanupExecution()
    if not proc.current_determinable():
        result.refuse(list(entries), "current session cannot be determined")
        return result
    if known is None:
        try:
            known = _gather_known(sessions or [], fresh=True)
        except OSError as exc:
            result.refuse(list(entries), f"liveness revalidation failed: {exc}")
            return result
    base_by_label = dict(_sid_dir_paths())
    for entry in entries:
        label, _, sid = entry.partition("/")
        base = base_by_label.get(label)
        if not base or not _is_child_name(sid):
            result.skip(entry, "not a previewable orphan path")
            continue
        if sid in known:
            result.skip(entry, "session is now protected")
            continue
        removal = _remove_path(os.path.join(base, sid))
        result.add_removal(removal)
        if removal.status is RemovalStatus.REMOVED:
            result.complete(entry)
        elif removal.status is RemovalStatus.MISSING:
            result.mark_missing(entry)
    return result


# --- Strategy A: pid-keyed zombie session files ----------------------------

def select_zombie_pids(session_procs: list[SessionProc], cur: set[int]) -> list[int]:
    """Removable `sessions/<pid>.json` pids — PURE (no IO), for unit tests.

    A pid file is removable iff its proc is CONFIRMED dead (`proc_alive is
    False` — the injected verdict) and the pid is neither the current session's
    nor a live one. For a resumed multi-pid sid this returns only the dead
    pid(s); the live pid's file is kept because its injected `proc_alive` is
    True. An UNINJECTED row (`proc_alive is None` — raw registry parse that
    never went through `liveness.live_session_procs`) is refused, not treated
    as dead: misusing this with raw rows must fail safe (delete nothing), not
    classify every session file as a zombie.
    """
    out: list[int] = []
    for sp in session_procs:
        if sp.pid in cur:               # current session's pid file — protected
            continue
        if sp.proc_alive is not False:  # alive, or uninjected (None) — keep
            continue
        out.append(sp.pid)
    return sorted(set(out))


def execute_zombie_removals(
    pids: list[int],
    *,
    session_procs: list[SessionProc] | None = None,
    cur: set[int] | None = None,
) -> CleanupExecution:
    """Delete AT MOST the previewed zombie `sessions/<pid>.json` files.

    Each pid is re-selected against FRESH liveness (`session_procs`/`cur`
    self-fetched when None) — a pid that came back alive (or became current)
    between preview and confirm is skipped. Refuses without `/proc` (R10).
    """
    result = CleanupExecution()
    if not proc.current_determinable():
        result.refuse(list(pids), "current session cannot be determined")
        return result
    if session_procs is None:
        try:
            session_procs = liveness.live_session_procs(max_age=0.0)
        except OSError as exc:
            result.refuse(list(pids), f"liveness revalidation failed: {exc}")
            return result
    if cur is None:
        cur = proc.ancestor_pids()
    still_zombie = set(select_zombie_pids(session_procs, cur))
    for pid in pids:
        if pid not in still_zombie:
            result.skip(pid, "session process is now live or current")
            continue
        removal = _remove_path(
            os.path.join(str(cfg.sessions_dir), f"{pid}.json")
        )
        result.add_removal(removal)
        if removal.status is RemovalStatus.REMOVED:
            result.complete(pid)
        elif removal.status is RemovalStatus.MISSING:
            result.mark_missing(pid)
    return result


# --- Strategy B: age sweep -------------------------------------------------

def _age_cutoff(now: float) -> float:
    return now - cfg.cleanup_age_days * _SECONDS_PER_DAY


def list_aged_entries(now: float | None = None) -> list[str]:
    """Age-swept entries (`<dir>/<name>`) older than `cfg.cleanup_age_days`."""
    cutoff = _age_cutoff(time.time() if now is None else now)
    out: list[str] = []
    for label, path in _age_dir_paths():
        try:
            names = os.listdir(path)
        except FileNotFoundError:
            continue
        for name in names:
            full = os.path.join(path, name)
            try:
                if os.lstat(full).st_mtime < cutoff:
                    out.append(os.path.join(label, name))
            except FileNotFoundError:
                continue
    return sorted(out)


def execute_aged_removals(
    entries: list[str], now: float | None = None
) -> CleanupExecution:
    """Delete AT MOST the previewed aged entries (`<label>/<name>`).

    Each entry's mtime is re-checked against the cutoff at execution time — an
    entry touched since the preview is skipped; entries that newly aged past
    the cutoff are NOT added (删除 ⊆ 预览). Mtime-only, so not R10-gated.
    """
    cutoff = _age_cutoff(time.time() if now is None else now)
    base_by_label = dict(_age_dir_paths())
    result = CleanupExecution()
    for entry in entries:
        label, _, name = entry.partition("/")
        base = base_by_label.get(label)
        if not base or not _is_child_name(name):
            result.skip(entry, "not a previewable aged-entry path")
            continue
        full = os.path.join(base, name)
        try:
            if os.lstat(full).st_mtime >= cutoff:
                result.skip(entry, "entry is no longer old enough")
                continue
        except FileNotFoundError:
            result.add_removal(PathRemoval(Path(full), RemovalStatus.MISSING))
            result.mark_missing(entry)
            continue
        except OSError as exc:
            result.add_removal(
                PathRemoval(Path(full), RemovalStatus.FAILED, str(exc))
            )
            continue
        result.add_removal(_remove_path(full))
        if result.removals[-1].status is RemovalStatus.REMOVED:
            result.complete(entry)
        elif result.removals[-1].status is RemovalStatus.MISSING:
            result.mark_missing(entry)
    return result


# --- Session prune + full delete -------------------------------------------

def prune_sessions(sessions: list[Session], max_prompts: int = 0) -> list[Session]:
    """Prunable sessions: not alive, not current, <= max_prompts, not recent.

    Refuses (returns []) when current can't be determined (R10): without `/proc`
    `current` is unreliable, so we must not propose deleting anything.
    """
    if not proc.current_determinable():
        return []
    alive_sids = {s.sid for s in sessions if s.alive}
    now = time.time()
    return [
        s for s in sessions
        if s.prompts <= max_prompts
        and s.sid not in alive_sids
        and not s.current
        and (now - s.mtime) > 600
    ]


def _session_is_protected(
    session: Session,
    session_procs: list[SessionProc],
    agents_map: dict[str, int | None],
    cur: set[int],
) -> bool:
    live_sids = {sp.sid for sp in session_procs if sp.proc_alive}
    live_sids |= {sid for sid, pid in agents_map.items() if pid}
    current_sids = {sp.sid for sp in session_procs if sp.pid in cur}
    return (
        session.sid in live_sids
        or session.sid in current_sids
        or session.current
        or bool(session.pid and session.pid in cur)
    )


def _remove_session_paths(
    session: Session, session_procs: list[SessionProc]
) -> CleanupExecution:
    result = CleanupExecution()
    paths: list[str] = []
    if session.file:
        paths.append(session.file)
        if session.file.endswith(".jsonl"):
            paths.append(session.file[:-6])
    paths.extend(_sid_keyed_paths(session.sid))
    for path in dict.fromkeys(paths):
        result.add_removal(_remove_path(path))

    _, host_alive = registry.host_pid_for_sid(session.sid, session_procs)
    if host_alive:
        result.skip(_jobs_path(session.sid), "background agent is live")
    else:
        result.add_removal(_remove_path(_jobs_path(session.sid)))
    if result.removed and not result.failed:
        result.complete(session.sid)
    elif not result.removed and not result.failed:
        result.mark_missing(session.sid)
    return result


def remove_session(s: Session) -> CleanupExecution:
    """Delete one session: its `.jsonl`, companion dir, and sid artifacts.

    Refuses when current can't be determined (R10) — without
    `/proc` we cannot prove `s` is not the launching session.

    M3: the `jobs/<short>` dir is removed ONLY when the sid has no LIVE host pid,
    so a live background worker's jobs dir is protected exactly like
    `agent_ops.remove_job` protects it (do not bypass the jobs/ guard).
    """
    if not proc.current_determinable():
        result = CleanupExecution()
        result.refuse([s.sid], "current session cannot be determined")
        return result
    try:
        session_procs, agents_map, cur = fresh_session_guards()
    except OSError as exc:
        result = CleanupExecution()
        result.refuse([s.sid], f"liveness revalidation failed: {exc}")
        return result
    if _session_is_protected(s, session_procs, agents_map, cur):
        result = CleanupExecution()
        result.skip(s.sid, "session is now live or current")
        return result
    return _remove_session_paths(s, session_procs)


def execute_session_removals(
    targets: list[Session],
    *,
    session_procs: list[SessionProc] | None = None,
    agents_map: dict[str, int | None] | None = None,
    cur: set[int] | None = None,
) -> CleanupExecution:
    """Delete AT MOST the previewed prunable sessions.

    Each target is revalidated against FRESH liveness (self-fetched when the
    kwargs are None): a session that came alive or became current between
    preview and confirm is skipped (删除 ⊆ 预览, 宁可少删). Refuses without
    `/proc` (R10).
    """
    result = CleanupExecution()
    if not proc.current_determinable():
        result.refuse(
            [s.sid for s in targets], "current session cannot be determined"
        )
        return result
    if session_procs is None or agents_map is None or cur is None:
        try:
            fresh_procs, fresh_agents, fresh_cur = fresh_session_guards()
        except OSError as exc:
            result.refuse(
                [s.sid for s in targets],
                f"liveness revalidation failed: {exc}",
            )
            return result
        if session_procs is None:
            session_procs = fresh_procs
        if agents_map is None:
            agents_map = fresh_agents
        if cur is None:
            cur = fresh_cur
    for s in targets:
        if _session_is_protected(s, session_procs, agents_map, cur):
            result.skip(s.sid, "session is now live or current")
            continue
        result.extend(_remove_session_paths(s, session_procs))
    return result


# --- The cleanup plan (ONE source for counts, preview, and execution) -------

_PlanItems = TypeVar("_PlanItems")


def _plan_source(
    source: str,
    load: Callable[[], _PlanItems],
    issues: list[CleanupIssue],
    empty: _PlanItems,
) -> _PlanItems:
    try:
        return load()
    except OSError as exc:
        issues.append(
            CleanupIssue(
                source=source,
                error=str(exc),
                path=os.fspath(exc.filename) if exc.filename else None,
            )
        )
        return empty


def build_plan(
    sessions: list[Session],
    session_procs: list[SessionProc],
    cur: set[int],
    agent_jobs: list[AgentJob] | None = None,
    agents_map: dict[str, int | None] | None = None,
    now: float | None = None,
) -> CleanupPlan:
    """Build the cleanup plan from the shared world data (deps injected)."""
    issues: list[CleanupIssue] = []
    return CleanupPlan(
        empty=prune_sessions(sessions, max_prompts=0),
        short=[s for s in prune_sessions(sessions, max_prompts=2) if s.prompts > 0],
        orphan_entries=_plan_source(
            "orphan_dirs",
            lambda: list_orphan_dirs(
                sessions,
                session_procs=session_procs,
                agent_jobs=agent_jobs,
                agents_map=agents_map,
                cur=cur,
            ),
            issues,
            [],
        ),
        zombie_pids=select_zombie_pids(session_procs, cur),
        aged_entries=_plan_source(
            "aged_entries", lambda: list_aged_entries(now), issues, []
        ),
        issues=issues,
    )
