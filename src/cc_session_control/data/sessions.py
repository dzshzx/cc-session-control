"""Session scanning — scan Claude Code transcripts and determine status."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace

from ..config import cfg
from ..models import LiveInfo, Session, SessionProc
from . import registry, tmux, transcripts
from .liveness import (
    LivenessSnapshot,
    alive_map,
    is_rc_exposed,
    live_index,
    live_session_procs,
)
from .proc import ancestor_pids as _ancestor_pids  # /proc walk moved to proc.py

TranscriptIssue = transcripts.TranscriptIssue


@dataclass(frozen=True)
class SessionScanResult:
    """Transcript-driven session rows plus source completeness."""

    sessions: tuple[Session, ...] = ()
    issues: tuple[TranscriptIssue, ...] = ()
    path_sids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "path_sids", frozenset(self.path_sids))

    @property
    def complete(self) -> bool:
        return not self.issues

    @property
    def sids(self) -> frozenset[str]:
        """Every discovered transcript sid — pathname-only ones included (F47)."""
        return self.path_sids | frozenset(row.sid for row in self.sessions)


def _project_transcript(
    transcript: transcripts.TranscriptRecord,
    idx: dict[str, LiveInfo],
    cur: AbstractSet[int],
    job_shorts: set[str],
) -> Session:
    """Project one parsed transcript through the captured liveness generation."""
    sid = transcript.sid
    # Join the merged liveness/identity for this sid. Missing => dead, no
    # registry data (transcript-only): liveness stays False and the registry-
    # derived fields stay empty.
    info = idx.get(sid)
    if info is not None:
        pid = info.pid
        alive = info.alive
        kind = info.kind
        entrypoint = info.entrypoint
        source = info.source
        status = info.status
        bridge = info.bridge
        # "current" must protect ANY of the sid's alive pids: a resumed session
        # has several pids and csctl may have been launched by an older one that
        # is NOT the newest `pid` chosen for display. Fall back to the single
        # chosen pid for hand-constructed LiveInfo with no `pids` list.
        cand = info.pids if info.pids else ([pid] if pid else [])
        current = any(p in cur for p in cand)
        proc_alive = info.proc_alive
        proc_start = info.proc_start
    else:
        pid = None
        alive = False
        kind = entrypoint = source = status = ""
        bridge = None
        current = False
        proc_alive = False
        proc_start = ""

    rc_exposed = is_rc_exposed(bridge, proc_alive)

    return Session(
        sid=sid,
        cwd=transcript.cwd,
        label=(
            transcript.title
            or transcript.first_prompt
            or transcript.last_prompt
            or "(untitled)"
        ),
        mtime=transcript.mtime,
        prompts=transcript.prompts,
        pid=pid,
        alive=alive,
        current=current,
        proc_start=proc_start,
        hidden=transcript.hidden,
        file=transcript.path,
        kind=kind,
        entrypoint=entrypoint,
        source=source,
        rc_exposed=rc_exposed,
        agent_short=sid[:8] if sid[:8] in job_shorts else None,
        status=status,
    )


def _candidate_pids(info: LiveInfo | None) -> tuple[int, ...]:
    """A LiveInfo's pid candidate set — `pids` when filled, else the chosen pid
    (same fallback rule the `current` check in `_project_transcript` uses)."""
    if info is None:
        return ()
    if info.pids:
        return info.pids
    return (info.pid,) if info.pid else ()


def _inject_tmux_residency(
    rows: list[Session],
    idx: dict[str, LiveInfo],
) -> list[Session]:
    """Fill `Session.tmux_target` for every ALIVE session, in ONE batch.

    Collects all alive sessions' candidate pids, calls
    `tmux.residency_inventory` once (one `list-panes -a` per scan cycle), and
    returns a replaced session carrying its first hit — any alive pid inside a
    tmux pane makes the session resident (ADR-0001). Dead sessions stay None;
    the badge and the resume/backgrounding actions read this SAME field, so
    there is no per-action re-detection (no second source of truth)."""
    alive_pids = {
        pid for row in rows if row.alive for pid in _candidate_pids(idx.get(row.sid))
    }
    inventory = tmux.residency_inventory(alive_pids)
    targets = inventory.targets
    if not targets and inventory.complete:
        return rows
    detail = "; ".join(
        f"{issue.source}"
        + (f" ({issue.path})" if issue.path else "")
        + f": {issue.detail}"
        for issue in inventory.issues
    )
    resident: list[Session] = []
    for row in rows:
        target = next(
            (
                targets[pid]
                for pid in _candidate_pids(idx.get(row.sid))
                if row.alive and pid in targets
            ),
            None,
        )
        if row.alive:
            resident.append(
                replace(
                    row,
                    tmux_target=target,
                    tmux_inventory_complete=inventory.complete,
                    tmux_inventory_detail=detail,
                )
            )
        else:
            resident.append(row)
    return resident


def scan_result(inputs: LivenessSnapshot | None = None) -> SessionScanResult:
    """Unified typed transcript-driven session scan.

    Merges the three liveness/identity sources once per scan — registry
    `sessions/<pid>.json`, `claude agents --json`, and `jobs/*/state.json` — then
    projects each transcript through `live_index()` to fill source/liveness/
    rc-exposure, and batch-injects tmux residency (`tmux_target`). Scan stays
    transcript-driven: an agent-only sid (present in the live index but with no
    transcript) is surfaced by the Agents tab, not here.

    When `inputs` is supplied by `build_world_snapshot`, this is an injected
    fast path: no registry or targeted `/proc` liveness read is repeated. With
    no injection, the standalone CLI-compatible path self-fetches as before.
    """
    root = os.fspath(cfg.projects_root)
    session_procs: Sequence[SessionProc]
    agents: Mapping[str, int | None]
    cur: AbstractSet[int]
    if inputs is None:
        session_procs = live_session_procs()
        agents = alive_map()
        job_shorts = {j.short for j in registry.read_agent_jobs()}
        cur = _ancestor_pids()
    else:
        session_procs = inputs.session_procs
        agents = inputs.agents_map
        job_shorts = {j.short for j in inputs.agent_jobs}
        cur = inputs.cur
    idx = live_index(session_procs, agents)
    rows: list[Session] = []

    inventory = transcripts.load_inventory(root)
    rows.extend(
        _project_transcript(transcript, idx, cur, job_shorts)
        for transcript in inventory.records
    )

    rows = _inject_tmux_residency(rows, idx)
    rows.sort(key=lambda r: r.mtime, reverse=True)
    return SessionScanResult(tuple(rows), inventory.issues, inventory.path_sids)
