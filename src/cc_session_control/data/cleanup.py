"""Preview-first cleanup policy bounded by frozen plans, fresh liveness, and
immutable anchors. `jobs/` is explicit-delete only; age cleanup is not R10-gated.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path

from ..config import cfg
from ..models import AgentJob, Session, SessionProc
from . import liveness, proc, registry
from . import sessions as session_data
from .cleanup_anchors import (
    PlanAnchors,
    pin_plan_targets,
)
from .cleanup_anchors import (
    agent_removal_anchors as _agent_removal_anchors,
)
from .cleanup_anchors import (
    entry_anchors as _raw_entry_anchors,
)
from .cleanup_anchors import (
    session_removal_anchors as _session_removal_anchors,
)
from .cleanup_liveness import (
    fill_liveness_inputs,
    fresh_liveness_inputs,
    refuse_incomplete_liveness,
)
from .cleanup_selection import known_sids
from .cleanup_selection import select_prunable_sessions as _select_prunable_sessions
from .removal import (
    CleanupExecution,
    CleanupIssue,
    CleanupPlan,
    PathRemoval,
    RemovalAnchor,
    RemovalStatus,
    anchor_path,
    inspect_anchored,
    remove_anchored,
)

# Dirs keyed by full sessionId — orphan = name not in the known sid set.
_SID_DIRS = ("session_env", "file_history", "tasks", "uploads")
# Dirs swept purely by mtime (not session-keyed).
_AGE_DIRS = ("shell_snapshots", "telemetry", "plans", "backups", "paste_cache")

_SECONDS_PER_DAY = 86400


def _is_child_name(name: str) -> bool:
    return name not in ("", ".", "..") and os.sep not in name


def _entry_anchors(
    entries: Sequence[str],
    bases: Mapping[str, str],
    result: CleanupExecution | None = None,
) -> dict[str, RemovalAnchor]:
    try:
        return _raw_entry_anchors(entries, bases)
    except OSError as exc:
        if result is None:
            raise
        result.refuse(entries, f"cannot establish removal anchor: {exc}")
        return {}


def _sid_dir_paths() -> list[tuple[str, str]]:
    """(label, path) for each sid-keyed directory, via cfg props only."""
    return [
        (name.replace("_", "-"), str(getattr(cfg, f"{name}_dir"))) for name in _SID_DIRS
    ]


def _age_dir_paths() -> list[tuple[str, str]]:
    """(label, path) for each age-swept directory, via cfg props only."""
    return [
        (name.replace("_", "-"), str(getattr(cfg, f"{name}_dir"))) for name in _AGE_DIRS
    ]


def _jobs_path(sid: str) -> str:
    """The 8-char-prefixed `jobs/<short>` dir for a session id."""
    return os.path.join(str(cfg.jobs_dir), sid[:8])


def agent_removal_anchors(short: str, sid: str) -> tuple[RemovalAnchor, ...]:
    return _agent_removal_anchors(
        short,
        sid,
        [base for _, base in _sid_dir_paths()],
        str(cfg.jobs_dir),
    )


def session_removal_anchors(
    sessions: Sequence[Session],
) -> dict[str, tuple[RemovalAnchor, ...]]:
    return _session_removal_anchors(
        sessions,
        [base for _, base in _sid_dir_paths()],
        str(cfg.jobs_dir),
    )


def _sid_is_protected(
    sid: str,
    session_procs: Sequence[SessionProc],
    agents_map: Mapping[str, int | None],
    cur: AbstractSet[int],
) -> bool:
    return (
        any(sp.sid == sid and sp.proc_alive for sp in session_procs)
        or bool(agents_map.get(sid))
        or any(sp.sid == sid and sp.pid in cur for sp in session_procs)
    )


def _remove_agent_artifact_paths(
    short: str,
    anchors: tuple[RemovalAnchor, ...],
) -> CleanupExecution:
    result = CleanupExecution()
    for anchor in anchors:
        result.add_removal(remove_anchored(anchor))
    if result.removed and not result.failed:
        result.complete(short)
    elif not result.removed and not result.incomplete:
        result.mark_missing(short)
    return result


def remove_agent_artifacts(
    short: str,
    sid: str,
    *,
    anchors: tuple[RemovalAnchor, ...] | None = None,
) -> CleanupExecution:
    """Delete anchored agent artifacts after fresh liveness revalidation."""
    result = CleanupExecution()
    try:
        pinned = anchors if anchors is not None else agent_removal_anchors(short, sid)
    except OSError as exc:
        result.refuse([short], f"cannot establish removal anchor: {exc}")
        return result
    evidence = fresh_liveness_inputs()
    if not evidence.complete:
        return refuse_incomplete_liveness(result, [short], evidence)
    if _sid_is_protected(
        sid,
        evidence.session_procs,
        evidence.agents_map,
        evidence.cur,
    ):
        result.skip(short, "background agent is now live or current")
        return result
    return _remove_agent_artifact_paths(short, pinned)


# --- Strategy A: sid-keyed orphan dirs (H1 protected-sid set) --------------


def _list_orphan_dirs_for_known(known: AbstractSet[str]) -> list[str]:
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


def _gather_known(
    sessions: Sequence[Session],
    session_procs: Sequence[SessionProc] | None = None,
    agent_jobs: Sequence[AgentJob] | None = None,
    agents_map: Mapping[str, int | None] | None = None,
    cur: AbstractSet[int] | None = None,
) -> set[str]:
    """Resolve protected sids, self-fetching omitted liveness inputs."""
    session_procs, agent_jobs, agents_map, cur = fill_liveness_inputs(
        session_procs, agent_jobs, agents_map, cur
    )
    return known_sids(sessions, session_procs, agent_jobs, agents_map, cur)


def list_orphan_dirs(
    sessions: Sequence[Session],
    *,
    session_procs: Sequence[SessionProc] | None = None,
    agent_jobs: Sequence[AgentJob] | None = None,
    agents_map: Mapping[str, int | None] | None = None,
    cur: AbstractSet[int] | None = None,
) -> list[str]:
    """Preview unprotected sid-keyed entries; return none in R10 degraded mode."""
    if not proc.probe_current_ancestors().complete:
        return []
    known = _gather_known(sessions, session_procs, agent_jobs, agents_map, cur)
    return _list_orphan_dirs_for_known(known)


def execute_orphan_removals(
    entries: list[str],
    *,
    anchors: Mapping[str, RemovalAnchor] | None = None,
) -> CleanupExecution:
    """Remove only anchored preview orphans after fresh protection revalidation."""
    result = CleanupExecution()
    base_by_label = dict(_sid_dir_paths())
    if anchors is None:
        anchors = _entry_anchors(entries, base_by_label, result)
    evidence = fresh_liveness_inputs()
    if not evidence.complete:
        return refuse_incomplete_liveness(result, entries, evidence)
    transcript_scan = session_data.scan_result(evidence)
    if not transcript_scan.complete:
        for issue in transcript_scan.issues:
            result.issues.append(CleanupIssue(issue.source, issue.detail, issue.path))
        result.refuse(entries, "transcript evidence incomplete; nothing deleted")
        return result
    known = known_sids(
        transcript_scan.sessions,
        evidence.session_procs,
        evidence.agent_jobs,
        evidence.agents_map,
        evidence.cur,
    )
    for entry in entries:
        label, _, sid = entry.partition("/")
        base = base_by_label.get(label)
        if not base or not _is_child_name(sid):
            result.skip(entry, "not a previewable orphan path")
            continue
        if sid in known:
            result.skip(entry, "session is now protected")
            continue
        anchor = anchors.get(entry)
        if anchor is None:
            result.refuse([entry], "removal anchor is missing from preview")
            continue
        removal = remove_anchored(anchor)
        result.add_removal(removal)
        if removal.status is RemovalStatus.REMOVED:
            result.complete(entry)
        elif removal.status is RemovalStatus.MISSING:
            result.mark_missing(entry)
    return result


# --- Strategy A: pid-keyed zombie session files ----------------------------


def select_zombie_pids(
    session_procs: Sequence[SessionProc],
    cur: AbstractSet[int],
) -> list[int]:
    """Select confirmed-dead, non-current pids; uninjected verdicts stay safe."""
    out: list[int] = []
    for sp in session_procs:
        if sp.pid in cur:  # current session's pid file — protected
            continue
        if sp.proc_alive is not False:  # alive, or uninjected (None) — keep
            continue
        out.append(sp.pid)
    return sorted(set(out))


def execute_zombie_removals(
    pids: list[int],
    *,
    anchors: Mapping[int, RemovalAnchor] | None = None,
) -> CleanupExecution:
    """Remove anchored preview zombies after fresh liveness revalidation."""
    result = CleanupExecution()
    if anchors is None:
        try:
            anchors = {
                pid: anchor_path(
                    cfg.sessions_dir,
                    cfg.sessions_dir / f"{pid}.json",
                )
                for pid in pids
            }
        except OSError as exc:
            result.refuse(pids, f"cannot establish removal anchor: {exc}")
            return result
    evidence = fresh_liveness_inputs()
    if not evidence.complete:
        return refuse_incomplete_liveness(result, pids, evidence)
    still_zombie = set(select_zombie_pids(evidence.session_procs, evidence.cur))
    for pid in pids:
        if pid not in still_zombie:
            result.skip(pid, "session process is now live or current")
            continue
        anchor = anchors.get(pid)
        if anchor is None:
            result.refuse([pid], "removal anchor is missing from preview")
            continue
        removal = remove_anchored(anchor)
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
    entries: list[str],
    now: float | None = None,
    *,
    anchors: Mapping[str, RemovalAnchor] | None = None,
) -> CleanupExecution:
    """Remove anchored preview entries still older than the cutoff."""
    cutoff = _age_cutoff(time.time() if now is None else now)
    base_by_label = dict(_age_dir_paths())
    result = CleanupExecution()
    if anchors is None:
        anchors = _entry_anchors(entries, base_by_label, result)
    for entry in entries:
        label, _, name = entry.partition("/")
        base = base_by_label.get(label)
        if not base or not _is_child_name(name):
            result.skip(entry, "not a previewable aged-entry path")
            continue
        anchor = anchors.get(entry)
        if anchor is None:
            result.refuse([entry], "removal anchor is missing from preview")
            continue
        inspection = inspect_anchored(anchor)
        if isinstance(inspection, PathRemoval):
            result.add_removal(inspection)
            if inspection.status is RemovalStatus.MISSING:
                result.mark_missing(entry)
            continue
        if inspection.st_mtime >= cutoff:
            result.skip(entry, "entry is no longer old enough")
            continue
        removal = remove_anchored(anchor)
        result.add_removal(removal)
        if removal.status is RemovalStatus.REMOVED:
            result.complete(entry)
        elif removal.status is RemovalStatus.MISSING:
            result.mark_missing(entry)
    return result


# --- Session prune + full delete -------------------------------------------


def prune_sessions(
    sessions: Sequence[Session],
    max_prompts: int = 0,
    *,
    evidence: liveness.LivenessSnapshot | None = None,
) -> list[Session]:
    """Prunable sessions: not alive, not current, <= max_prompts, not recent.

    Uses an injected complete generation without acquiring sources. Compatibility
    callers self-probe and refuse when current cannot be determined (R10).
    """
    if evidence is None:
        protection_complete = proc.probe_current_ancestors().complete
    else:
        protection_complete = evidence.complete
    if not protection_complete:
        return []
    return _select_prunable_sessions(sessions, max_prompts, time.time())


def _session_is_protected(
    session: Session,
    session_procs: Sequence[SessionProc],
    agents_map: Mapping[str, int | None],
    cur: AbstractSet[int],
) -> bool:
    return (
        _sid_is_protected(session.sid, session_procs, agents_map, cur)
        or session.current
        or bool(session.pid and session.pid in cur)
    )


def _remove_session_paths(
    session: Session,
    session_procs: list[SessionProc],
    anchors: tuple[RemovalAnchor, ...],
) -> CleanupExecution:
    result = CleanupExecution()
    _, host_alive = registry.host_pid_for_sid(session.sid, session_procs)
    jobs_target = Path(os.path.abspath(_jobs_path(session.sid)))
    for anchor in anchors:
        if anchor.configured_target == jobs_target and host_alive:
            result.skip(jobs_target, "background agent is live")
            continue
        result.add_removal(remove_anchored(anchor))
    if result.removed and not result.failed:
        result.complete(session.sid)
    elif not result.removed and not result.incomplete:
        result.mark_missing(session.sid)
    return result


def remove_session(
    s: Session,
    *,
    anchors: tuple[RemovalAnchor, ...] | None = None,
) -> CleanupExecution:
    """Delete anchored session artifacts after fresh R10/M3 protection gates."""
    result = CleanupExecution()
    try:
        pinned = anchors if anchors is not None else session_removal_anchors([s])[s.sid]
    except OSError as exc:
        result.refuse([s.sid], f"cannot establish removal anchor: {exc}")
        return result
    evidence = fresh_liveness_inputs()
    if not evidence.complete:
        return refuse_incomplete_liveness(CleanupExecution(), [s.sid], evidence)
    session_procs = list(evidence.session_procs)
    agents_map = dict(evidence.agents_map)
    cur = set(evidence.cur)
    if _session_is_protected(s, session_procs, agents_map, cur):
        result.skip(s.sid, "session is now live or current")
        return result
    return _remove_session_paths(s, session_procs, pinned)


def execute_session_removals(
    targets: list[Session],
    *,
    anchors: Mapping[str, tuple[RemovalAnchor, ...]] | None = None,
) -> CleanupExecution:
    """Remove only anchored preview sessions after fresh liveness revalidation."""
    result = CleanupExecution()
    if anchors is None:
        try:
            anchors = session_removal_anchors(targets)
        except OSError as exc:
            result.refuse(
                [s.sid for s in targets],
                f"cannot establish removal anchor: {exc}",
            )
            return result
    evidence = fresh_liveness_inputs()
    if not evidence.complete:
        return refuse_incomplete_liveness(
            result,
            [s.sid for s in targets],
            evidence,
        )
    session_procs = list(evidence.session_procs)
    agents_map = dict(evidence.agents_map)
    cur = set(evidence.cur)
    for s in targets:
        if _session_is_protected(s, session_procs, agents_map, cur):
            result.skip(s.sid, "session is now live or current")
            continue
        pinned = anchors.get(s.sid)
        if pinned is None:
            result.refuse([s.sid], "removal anchor is missing from preview")
            continue
        result.extend(_remove_session_paths(s, session_procs, pinned))
    return result


# --- The cleanup plan (ONE source for counts, preview, and execution) -------


def _plan_source[PlanItems](
    source: str,
    load: Callable[[], PlanItems],
    issues: list[CleanupIssue],
    empty: PlanItems,
) -> PlanItems:
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
    sessions: Sequence[Session],
    evidence: liveness.LivenessSnapshot,
    now: float | None = None,
) -> CleanupPlan:
    """Build one cleanup plan solely from verified generation evidence."""
    if not evidence.complete:
        raise ValueError("cleanup plan requires complete liveness evidence")

    plan_now = time.time() if now is None else now
    issues: list[CleanupIssue] = []
    empty = _select_prunable_sessions(sessions, max_prompts=0, now=plan_now)
    short = [
        s
        for s in _select_prunable_sessions(sessions, max_prompts=2, now=plan_now)
        if s.prompts > 0
    ]
    candidates = list({s.sid: s for s in [*empty, *short]}.values())
    protected_sids = known_sids(
        sessions,
        evidence.session_procs,
        evidence.agent_jobs,
        evidence.agents_map,
        evidence.cur,
    )
    orphan_entries: list[str] = _plan_source(
        "orphan_dirs",
        lambda: _list_orphan_dirs_for_known(protected_sids),
        issues,
        [],
    )
    zombie_pids: list[int] = select_zombie_pids(
        evidence.session_procs,
        evidence.cur,
    )
    aged_entries: list[str] = _plan_source(
        "aged_entries", lambda: list_aged_entries(plan_now), issues, []
    )
    pinned: PlanAnchors = _plan_source(
        "removal_anchors",
        lambda: pin_plan_targets(
            candidates,
            orphan_entries,
            dict(_sid_dir_paths()),
            zombie_pids,
            str(cfg.sessions_dir),
            aged_entries,
            dict(_age_dir_paths()),
            [base for _, base in _sid_dir_paths()],
            str(cfg.jobs_dir),
        ),
        issues,
        PlanAnchors({}, {}, {}, {}),
    )
    return CleanupPlan(
        empty=tuple(s for s in empty if s.sid in pinned.sessions),
        short=tuple(s for s in short if s.sid in pinned.sessions),
        orphan_entries=tuple(
            entry for entry in orphan_entries if entry in pinned.orphans
        ),
        zombie_pids=tuple(pid for pid in zombie_pids if pid in pinned.zombies),
        aged_entries=tuple(entry for entry in aged_entries if entry in pinned.aged),
        issues=tuple(issues),
        session_anchors=pinned.sessions,
        orphan_anchors=pinned.orphans,
        zombie_anchors=pinned.zombies,
        aged_anchors=pinned.aged,
    )
