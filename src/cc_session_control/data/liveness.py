"""Session liveness — the single authority.

Owns the ONE module-global cache for `claude agents --json`.
`live_index` is a pure merge of already-fetched liveness inputs (registry
session files with injected proc liveness + `claude agents --json`).
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from ..models import AgentJob, LiveInfo, SessionProc
from . import proc, registry


class AgentsAvailability(StrEnum):
    """How much of `claude agents --json` was available to one scan."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AgentsIssue:
    """One expected `claude agents --json` failure."""

    source: str
    path: str | None
    detail: str


@dataclass(frozen=True)
class AgentsScan:
    """Agent liveness records plus their source completeness."""

    records: Mapping[str, int | None] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    issues: tuple[AgentsIssue, ...] = ()
    availability: AgentsAvailability = AgentsAvailability.AVAILABLE

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", MappingProxyType(dict(self.records)))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def complete(self) -> bool:
        return self.availability is AgentsAvailability.AVAILABLE and not self.issues


_cache: AgentsScan | None = None
_cache_time: float = 0.0


@dataclass(frozen=True)
class LivenessIssue:
    """One incomplete protection source in a generation snapshot."""

    source: str
    path: str | None
    detail: str


@dataclass(frozen=True)
class LivenessSnapshot:
    """Immutable liveness evidence captured for exactly one refresh generation."""

    session_procs: tuple[SessionProc, ...] = ()
    cur: frozenset[int] = frozenset()
    agent_jobs: tuple[AgentJob, ...] = ()
    agents_map: Mapping[str, int | None] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    issues: tuple[LivenessIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_procs", tuple(self.session_procs))
        object.__setattr__(self, "cur", frozenset(self.cur))
        object.__setattr__(self, "agent_jobs", tuple(self.agent_jobs))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(
            self,
            "agents_map",
            MappingProxyType(dict(self.agents_map)),
        )

    @property
    def complete(self) -> bool:
        return not self.issues


def _probe_proc_liveness(
    session_procs: Sequence[SessionProc],
) -> tuple[list[SessionProc], list[LivenessIssue]]:
    records: list[SessionProc] = []
    issues: list[LivenessIssue] = []
    for session_proc in session_procs:
        probe = proc.probe_pid(session_proc.pid, session_proc.proc_start)
        records.append(replace(session_proc, proc_alive=probe.alive))
        if probe.issue is not None:
            issues.append(
                LivenessIssue(
                    source=probe.issue.source,
                    path=probe.issue.path,
                    detail=probe.issue.detail,
                )
            )
    return records, issues


def _inject_proc_liveness(
    session_procs: Sequence[SessionProc],
) -> list[SessionProc]:
    """Compatibility records-only view; safety decisions use the typed snapshot."""
    return _probe_proc_liveness(session_procs)[0]


def _registry_issues(
    issues: Sequence[registry.RegistryIssue],
) -> list[LivenessIssue]:
    return [
        LivenessIssue(
            source=issue.source,
            path=issue.path,
            detail=issue.detail,
        )
        for issue in issues
    ]


def liveness_inputs() -> LivenessSnapshot:
    """Capture the shared liveness inputs once for one caller generation.

    `build_world_snapshot` and `cleanup`'s protection-set assembly consume this,
    so the refresh generation and cleanup protection set share one assembly
    instead of a hand-kept mirror that can drift. Expected source failures are
    handled by their owning readers.

    The source registries may have their own caches for non-generation callers,
    but a refresh generation always asks for fresh source evidence. In
    particular, the process verdicts in `agents_map` must never be reused as if
    they described a new generation.
    """
    session_scan = registry.scan_session_procs(max_age=0.0)
    session_procs, proc_issues = _probe_proc_liveness(session_scan.records)
    jobs_scan = registry.scan_agent_jobs(max_age=0.0)
    agent_jobs = enrich_jobs(
        jobs_scan.records,
        session_procs,
    )
    agents_scan = scan_agents(max_age=0.0)
    ancestors = proc.probe_current_ancestors()
    issues = [
        *_registry_issues(session_scan.issues),
        *_registry_issues(jobs_scan.issues),
        *proc_issues,
        *(
            LivenessIssue(issue.source, issue.path, issue.detail)
            for issue in ancestors.issues
        ),
        *(
            LivenessIssue(issue.source, issue.path, issue.detail)
            for issue in agents_scan.issues
        ),
    ]
    return LivenessSnapshot(
        session_procs=tuple(session_procs),
        cur=ancestors.pids,
        agent_jobs=tuple(agent_jobs),
        agents_map=agents_scan.records,
        issues=tuple(issues),
    )


def _scrub_dead_pids(
    mapping: dict[str, int | None],
    exists: Callable[[int | None], bool],
) -> dict[str, int | None]:
    """Blank out pids whose process no longer exists (pure; `exists` injected).

    `claude agents --json` can keep reporting a pid after the worker died
    (SIGKILL/crash before its registry caught up). A dead pid must not count as
    alive — blanking it lets the existing "pid-less entries are not alive" rule
    in `live_index` take over. Entries are kept, never dropped.
    """
    return {sid: (pid if exists(pid) else None) for sid, pid in mapping.items()}


def _probe_agent_pids(
    mapping: Mapping[str, int | None],
) -> tuple[dict[str, int | None], list[AgentsIssue]]:
    """Scrub only confirmed-gone pids and retain unknown evidence."""
    records: dict[str, int | None] = {}
    issues: list[AgentsIssue] = []
    for sid, pid in mapping.items():
        probe = proc.probe_pid(pid, None)
        records[sid] = None if probe.alive is False else pid
        if probe.issue is not None:
            issues.append(
                AgentsIssue(
                    probe.issue.source,
                    probe.issue.path,
                    probe.issue.detail,
                )
            )
    return records, issues


def _agents_failure(detail: str) -> AgentsScan:
    issue = AgentsIssue("claude agents --json", None, detail)
    return AgentsScan(
        issues=(issue,),
        availability=AgentsAvailability.UNAVAILABLE,
    )


def scan_agents(max_age: float = 5.0) -> AgentsScan:
    """Scan `claude agents --json`, retaining typed availability and issues.

    With `/proc` available, pids are checked against typed process evidence at
    cache-refresh time (see `_probe_agent_pids`). Confirmed-gone pids are
    scrubbed; unknown pids and their issues are retained.
    """
    global _cache, _cache_time
    now = time.monotonic()
    if _cache is not None and (now - _cache_time) < max_age:
        return _cache
    try:
        completed = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        result = _agents_failure(str(exc))
    except (OSError, UnicodeError) as exc:
        result = _agents_failure(str(exc))
    else:
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            detail = f"exit status {completed.returncode}"
            if stderr:
                detail += f": {stderr}"
            result = _agents_failure(detail)
        else:
            try:
                document = json.loads(completed.stdout or "[]")
            except json.JSONDecodeError as exc:
                result = _agents_failure(f"invalid JSON: {exc}")
            except UnicodeError as exc:
                result = _agents_failure(f"invalid Unicode: {exc}")
            else:
                records: dict[str, int | None] = {}
                issues: list[AgentsIssue] = []
                if not isinstance(document, list):
                    result = _agents_failure(
                        "invalid schema: expected a JSON array",
                    )
                else:
                    for index, entry in enumerate(document):
                        if not isinstance(entry, dict):
                            issues.append(
                                AgentsIssue(
                                    "claude agents --json",
                                    None,
                                    f"invalid schema at entry {index}: "
                                    "expected an object",
                                )
                            )
                            continue
                        sid = entry.get("sessionId")
                        pid = entry.get("pid")
                        if (
                            not isinstance(sid, str)
                            or not sid
                            or (pid is not None and not isinstance(pid, int))
                        ):
                            issues.append(
                                AgentsIssue(
                                    "claude agents --json",
                                    None,
                                    f"invalid schema at entry {index}: sessionId/pid",
                                )
                            )
                            continue
                        records[sid] = pid
                    availability = (
                        AgentsAvailability.PARTIAL
                        if issues
                        else AgentsAvailability.AVAILABLE
                    )
                    result = AgentsScan(
                        records=records,
                        issues=tuple(issues),
                        availability=availability,
                    )
    if proc.has_proc():
        records, proc_issues = _probe_agent_pids(result.records)
        availability = result.availability
        if proc_issues and availability is AgentsAvailability.AVAILABLE:
            availability = AgentsAvailability.PARTIAL
        result = AgentsScan(
            records=records,
            issues=(*result.issues, *proc_issues),
            availability=availability,
        )
    _cache = result
    _cache_time = now
    return result


def alive_map(max_age: float = 5.0) -> dict[str, int | None]:
    """Compatibility records-only view of :func:`scan_agents`."""
    return dict(scan_agents(max_age=max_age).records)


def invalidate_cache() -> None:
    global _cache
    _cache = None


def live_session_procs(max_age: float = 5.0) -> list[SessionProc]:
    """Registry session files with `/proc` liveness injected — THE assembly point.

    `registry.read_session_procs` deliberately leaves `proc_alive=None` (pure
    parse, no `/proc`). Injection supplies ``True`` or ``False`` only for
    conclusive probes and preserves ``None`` for unknown liveness. Security
    consumers use :func:`liveness_inputs` so the matching issues and completeness
    bit are not lost. Expected I/O failures are typed; programming errors
    propagate.
    """
    return _inject_proc_liveness(registry.read_session_procs(max_age=max_age))


def enrich_jobs(
    jobs: Sequence[AgentJob],
    session_procs: Sequence[SessionProc] | None = None,
) -> list[AgentJob]:
    """Fill each job's `host_pid`/`host_alive` — THE one enrich loop.

    `state.json` carries no pid, so a worker's host pid is JOINed from
    `sessions/<pid>.json` via the single pure join `registry.host_pid_for_sid`.
    The snapshot, the agents view's self-fetch path, and `csctl agents` all
    consume this loop: pass the shared snapshot's `session_procs` for zero
    extra IO, or `None` to self-fetch ONCE (the old per-job `job_host` loops
    re-injected `/proc` liveness onto the whole registry once per job).
    Returns fresh copies so the ~5s-TTL cached registry objects are never
    mutated.
    """
    if session_procs is None:
        session_procs = live_session_procs()
    out: list[AgentJob] = []
    for job in jobs:
        pid, alive = registry.host_pid_for_sid(job.sid, session_procs)
        out.append(replace(job, host_pid=pid, host_alive=alive))
    return out


def _source_of(entrypoint: str, kind: str) -> str:
    """Coarse source bucket from the registry entrypoint/kind (D9)."""
    if kind == "bg":
        return "bg"
    if entrypoint == "claude-vscode":
        return "vscode"
    if entrypoint == "sdk-ts":
        return "sdk"
    return "cli"


def is_rc_exposed(bridge: str | None, pid_alive: bool) -> bool:
    """Whether session remote control is CURRENTLY exposed (pure predicate).

    Exposed iff the bridge id is a truthy string AND the owning process is still
    alive. This correctly collapses the three bridge states — key absent (None),
    opened-then-closed (null/None, transient), and exposing (a `session_*`
    string) — crossed with alive/dead. The single authority for "currently
    exposed" (R3/AC3). No IO; inputs injected.
    """
    return bool(bridge) and pid_alive


def _start_key(proc_start: str) -> int:
    try:
        return int(proc_start)
    except (TypeError, ValueError):
        return -1


def live_index(
    session_procs: Sequence[SessionProc],
    agents_map: Mapping[str, int | None],
) -> dict[str, LiveInfo]:
    """PURE merge of registry session files + `claude agents --json`.

    Groups `session_procs` by sessionId (resume keeps the sid, mints a new pid),
    picks the injected proc-alive pid (newest `procStart` when several), and
    marks liveness. Falls back to `agents_map` when there is no proc-confirmed
    runtime — on non-Linux all `proc_alive` values are False, so a sid present in
    `agents_map` is still reported alive (degraded liveness). No IO; inputs are
    injected.
    """
    index: dict[str, LiveInfo] = {}

    by_sid: dict[str, list[SessionProc]] = {}
    for sp in session_procs:
        by_sid.setdefault(sp.sid, []).append(sp)

    for sid, procs in by_sid.items():
        alive_procs = [p for p in procs if p.proc_alive]
        if alive_procs:
            chosen = max(alive_procs, key=lambda p: _start_key(p.proc_start))
            alive = True
            # All alive pids, not just the newest — "current" must protect any
            # ancestor pid of a resumed (multi-pid) sid.
            pids = tuple(p.pid for p in alive_procs)
        else:
            chosen = max(procs, key=lambda p: _start_key(p.proc_start))
            alive = False
            pids = ()
        index[sid] = LiveInfo(
            sid=sid,
            pid=chosen.pid if alive else None,
            proc_start=chosen.proc_start,
            status=chosen.status,
            kind=chosen.kind,
            entrypoint=chosen.entrypoint,
            bridge=chosen.bridge,
            source=_source_of(chosen.entrypoint, chosen.kind),
            alive=alive,
            proc_alive=alive,
            pids=pids,
        )

    # `claude agents --json` is authoritative for liveness: it covers agent-only
    # sids and rescues the degraded (no-/proc) path. But it also keeps listing
    # settled/blocked bg sessions WITHOUT a pid — those are not alive (there is
    # no process to signal; terminate/stop would always fail), so alive is
    # judged by pid non-empty.
    for sid, pid in agents_map.items():
        if not sid:
            continue
        info = index.get(sid)
        if info is None:
            index[sid] = LiveInfo(
                sid=sid,
                pid=pid,
                alive=bool(pid),
                pids=(pid,) if pid else (),
            )
            continue
        if not pid:
            continue  # pid-less entry: the proc-based verdict stands
        index[sid] = replace(
            info,
            alive=True,
            pid=info.pid if info.pid is not None else pid,
            pids=info.pids if pid in info.pids else (*info.pids, pid),
        )
    return index
