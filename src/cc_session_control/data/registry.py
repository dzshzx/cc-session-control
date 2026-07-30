"""Read Claude Code's session/agent registries with typed scan diagnostics.

Two on-disk registers Claude Code maintains itself:
  - `sessions/<pid>.json`  → one per local runtime (a sid can have several)
  - `jobs/<short>/state.json` → one per background agent (NO pid inside)

The typed scanners retain partial records and every expected source issue.
Compatibility readers return only the records for non-safety callers. Results
are cached for ~5s; pass `max_age=0.0` for fresh protection evidence.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..config import cfg
from ..models import AgentJob, InventoryIssue, SessionProc, split_env_id

#: Same (source, path, detail) record everywhere — one canonical issue type.
RegistryIssue = InventoryIssue


@dataclass(frozen=True)
class RegistryScan[Record]:
    """Records plus the completeness of their registry source."""

    records: tuple[Record, ...] = ()
    issues: tuple[RegistryIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


_sessions_cache: RegistryScan[SessionProc] | None = None
_sessions_time: float = 0.0
_jobs_cache: RegistryScan[AgentJob] | None = None
_jobs_time: float = 0.0


def invalidate_cache() -> None:
    """Drop both cached reads (next call re-scans disk)."""
    global _sessions_cache, _jobs_cache
    _sessions_cache = None
    _jobs_cache = None


def _read_document(path: str) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_session_proc(document: object) -> tuple[SessionProc | None, str | None]:
    d = document
    if not isinstance(d, dict):
        return None, "invalid schema: expected a JSON object"
    sid = d.get("sessionId")
    pid = d.get("pid")
    if not sid or not pid:
        return None, "invalid schema: sessionId and pid are required"
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None, "invalid schema: pid must be an integer"
    bridge = d.get("bridgeSessionId")
    if bridge is not None and not isinstance(bridge, str):
        return None, "invalid schema: bridgeSessionId must be a string or null"
    return (
        SessionProc(
            pid=pid_int,
            sid=str(sid),
            cwd=d.get("cwd", "") or "",
            kind=d.get("kind", "") or "",
            entrypoint=d.get("entrypoint", "") or "",
            status=d.get("status", "") or "",
            proc_start=str(d.get("procStart", "") or ""),
            bridge=bridge,
        ),
        None,
    )


def _root_paths(
    root: str,
    source: str,
    select: Callable[[os.DirEntry[str]], bool],
) -> tuple[list[str], RegistryIssue | None]:
    try:
        with os.scandir(root) as entries:
            paths = [entry.path for entry in entries if select(entry)]
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], RegistryIssue(source, root, str(exc))
    return sorted(paths), None


def _scan_records[Record](
    paths: Sequence[str],
    source: str,
    parse: Callable[[object], tuple[Record | None, str | None]],
) -> tuple[list[Record], list[RegistryIssue]]:
    rows: list[Record] = []
    issues: list[RegistryIssue] = []
    for path in paths:
        try:
            document = _read_document(path)
        except FileNotFoundError:
            # State files are replaced asynchronously; disappearance after
            # directory enumeration is an expected race, not evidence loss.
            continue
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            issues.append(RegistryIssue(source, path, str(exc)))
            continue
        row, schema_error = parse(document)
        if schema_error is not None:
            issues.append(RegistryIssue(source, path, schema_error))
        elif row is not None:
            rows.append(row)
    return rows, issues


def scan_session_procs(max_age: float = 5.0) -> RegistryScan[SessionProc]:
    """Scan `sessions/*.json`, retaining partial records and source issues."""
    global _sessions_cache, _sessions_time
    now = time.monotonic()
    if _sessions_cache is not None and (now - _sessions_time) < max_age:
        return _sessions_cache
    root = os.fspath(cfg.sessions_dir)
    paths, root_issue = _root_paths(
        root,
        "session registry",
        lambda entry: entry.name.endswith(".json") and entry.is_file(),
    )
    if root_issue is not None:
        result = RegistryScan[SessionProc](issues=(root_issue,))
    else:
        rows, issues = _scan_records(
            paths,
            "session registry",
            _parse_session_proc,
        )
        result = RegistryScan(
            records=tuple(rows),
            issues=tuple(issues),
        )
    _sessions_cache = result
    _sessions_time = now
    return result


def read_session_procs(max_age: float = 5.0) -> list[SessionProc]:
    """Compatibility records-only view of :func:`scan_session_procs`."""
    return list(scan_session_procs(max_age=max_age).records)


def _parse_agent_job(
    document: object,
    *,
    short: str,
) -> tuple[AgentJob | None, str | None]:
    if not short:
        return None, "invalid schema: job directory name is empty"
    d = document
    if not isinstance(d, dict):
        return None, "invalid schema: expected a JSON object"
    sid_value = d.get("sessionId")
    if not isinstance(sid_value, str) or not sid_value:
        return None, "invalid schema: sessionId is required"
    sid = sid_value
    flags = d.get("respawnFlags")
    if not isinstance(flags, list):
        flags = []
    bridge = d.get("bridgeSessionId")
    if bridge is not None and not isinstance(bridge, str):
        return None, "invalid schema: bridgeSessionId must be a string or null"
    return (
        AgentJob(
            short=short,
            sid=sid,
            resume_sid=str(d.get("resumeSessionId") or sid),
            state=d.get("state", "") or "",
            tempo=d.get("tempo", "") or "",
            cwd=d.get("cwd", "") or "",
            name=d.get("name", "") or "",
            env_suffix=split_env_id(bridge)[1],
            respawn_flags=tuple(str(x) for x in flags),
        ),
        None,
    )


def scan_agent_jobs(max_age: float = 5.0) -> RegistryScan[AgentJob]:
    """Scan `jobs/*/state.json`, retaining partial records and source issues."""
    global _jobs_cache, _jobs_time
    now = time.monotonic()
    if _jobs_cache is not None and (now - _jobs_time) < max_age:
        return _jobs_cache
    root = os.fspath(cfg.jobs_dir)
    job_dirs, root_issue = _root_paths(
        root,
        "job registry",
        lambda entry: entry.is_dir(),
    )
    if root_issue is not None:
        result = RegistryScan[AgentJob](issues=(root_issue,))
    else:
        rows: list[AgentJob] = []
        issues: list[RegistryIssue] = []
        for job_dir in job_dirs:
            state_path = os.path.join(job_dir, "state.json")
            try:
                document = _read_document(state_path)
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                issues.append(RegistryIssue("job registry", state_path, str(exc)))
                continue
            row, schema_error = _parse_agent_job(
                document,
                short=os.path.basename(job_dir),
            )
            if schema_error is not None:
                issues.append(RegistryIssue("job registry", state_path, schema_error))
            elif row is not None:
                rows.append(row)
        result = RegistryScan(
            records=tuple(rows),
            issues=tuple(issues),
        )
    _jobs_cache = result
    _jobs_time = now
    return result


def read_agent_jobs(max_age: float = 5.0) -> list[AgentJob]:
    """Compatibility records-only view of :func:`scan_agent_jobs`."""
    return list(scan_agent_jobs(max_age=max_age).records)


def host_pid_for_sid(
    sid: str, session_procs: Sequence[SessionProc]
) -> tuple[int | None, bool]:
    """Join a sid to its host pid via the registry session files — PURE.

    `jobs/<short>/state.json` carries NO pid, so a background/agent worker's host
    pid is the `sessions/<pid>.json` entry sharing the sid. Prefers a proc-alive
    match (`proc_alive is True` — so `alive=True` is trustworthy and defeats pid
    reuse); falls back to the first sid match with `alive=False`. An uninjected
    row (`proc_alive is None`) can never yield `alive=True` — only
    `liveness.live_session_procs` injection can. Returns `(None, False)` when
    no sessions file references the sid (that live worker is unstoppable).

    The single host-pid join shared by `liveness.enrich_jobs` and
    `cleanup.remove_session` (M3 guard).
    """
    procs = [sp for sp in session_procs if sp.sid == sid]
    if not procs:
        return None, False
    for sp in procs:
        if sp.proc_alive is True:
            return sp.pid, True
    return procs[0].pid, False
