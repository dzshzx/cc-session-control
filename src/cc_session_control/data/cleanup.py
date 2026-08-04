"""Preview-first cleanup policy bounded by frozen plans, fresh liveness, and
immutable anchors. `jobs/` is explicit-delete only; age cleanup is not R10-gated.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path

from ..config import cfg
from ..models import AgentJob, Session, SessionProc
from . import liveness, proc, registry, transcripts
from .age_cleanup import (
    AgeCleanupPlan,
    build_age_plan,
)
from .age_cleanup import (
    execute_aged_removals as execute_aged_removals,
)
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
from .removal import (
    CleanupExecution,
    CleanupIssue,
    CleanupPlan,
    RemovalAnchor,
    RemovalStatus,
    anchor_path,
    remove_anchored,
)

# Dirs keyed by full sessionId — orphan = name not in the known sid set.
_SID_DIRS = ("session_env", "file_history", "tasks", "uploads")


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


def _select_prunable_sessions(
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


def fresh_liveness_inputs() -> liveness.LivenessSnapshot:
    """Read every cleanup protection source with its cache disabled."""
    return liveness.liveness_inputs()


def refuse_incomplete_liveness(
    result: CleanupExecution,
    targets: Sequence[object],
    evidence: liveness.LivenessSnapshot,
) -> CleanupExecution:
    """Fail closed while retaining every unavailable protection source."""
    result.issues.extend(
        CleanupIssue(
            source=issue.source,
            error=issue.detail,
            path=issue.path,
        )
        for issue in evidence.issues
    )
    result.refuse(
        list(targets) or ["liveness evidence"],
        "liveness evidence incomplete; nothing deleted",
    )
    return result


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
    transcript_inventory = transcripts.load_inventory(str(cfg.projects_root))
    if not transcript_inventory.complete:
        for issue in transcript_inventory.issues:
            result.issues.append(CleanupIssue(issue.source, issue.detail, issue.path))
        result.refuse(entries, "transcript evidence incomplete; nothing deleted")
        return result
    known = known_sids_from_transcripts(
        transcript_inventory.sids,
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
    if s.provider != "claude":
        # Cleanup models Claude state only (ADR-0005): a non-Claude row's
        # `file` anchor points INTO the owning CLI's own store (codex rollout,
        # kimi state.json) — csctl never deletes state it does not fully model.
        # The codex `d` path (`providers.execute_cli_delete` → official
        # `codex delete`) is a typed bypass BESIDE this boundary, not a
        # relaxation of it — this seam still refuses every non-Claude row.
        result.refuse(
            [s.sid],
            f"provider {s.provider!r} sessions are not csctl-deletable",
        )
        return result
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
    *,
    transcript_sids: AbstractSet[str],
    age_plan: AgeCleanupPlan | None = None,
) -> CleanupPlan:
    """Build one cleanup plan solely from verified generation evidence."""
    if not evidence.complete:
        raise ValueError("cleanup plan requires complete liveness evidence")

    plan_now = time.time() if now is None else now
    captured_age = build_age_plan(plan_now) if age_plan is None else age_plan
    issues: list[CleanupIssue] = []
    empty = _select_prunable_sessions(sessions, max_prompts=0, now=plan_now)
    short = [
        s
        for s in _select_prunable_sessions(sessions, max_prompts=2, now=plan_now)
        if s.prompts > 0
    ]
    candidates = list({s.sid: s for s in [*empty, *short]}.values())
    # F47: pathname-only sids (empty/no-cwd transcripts) protect the preview
    # exactly like the execute side — plan truthfulness, 删除 ⊆ 预览.
    protected_sids = known_sids_from_transcripts(
        (s.sid for s in sessions),
        evidence.session_procs,
        evidence.agent_jobs,
        evidence.agents_map,
        evidence.cur,
    ) | set(transcript_sids)
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
    pinned: PlanAnchors = _plan_source(
        "removal_anchors",
        lambda: pin_plan_targets(
            candidates,
            orphan_entries,
            dict(_sid_dir_paths()),
            zombie_pids,
            str(cfg.sessions_dir),
            [base for _, base in _sid_dir_paths()],
            str(cfg.jobs_dir),
        ),
        issues,
        PlanAnchors({}, {}, {}),
    )
    session_plan = CleanupPlan(
        empty=tuple(s for s in empty if s.sid in pinned.sessions),
        short=tuple(s for s in short if s.sid in pinned.sessions),
        orphan_entries=tuple(
            entry for entry in orphan_entries if entry in pinned.orphans
        ),
        zombie_pids=tuple(pid for pid in zombie_pids if pid in pinned.zombies),
        issues=tuple(issues),
        session_anchors=pinned.sessions,
        orphan_anchors=pinned.orphans,
        zombie_anchors=pinned.zombies,
    )
    return captured_age.to_cleanup_plan(session_plan)
