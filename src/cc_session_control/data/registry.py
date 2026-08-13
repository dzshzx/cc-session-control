"""Read Claude Code's session registry with typed scan diagnostics.

The on-disk register Claude Code maintains itself:
  - `sessions/<pid>.json`  → one per local runtime (a sid can have several)

The typed scanner retains partial records and every expected source issue.
The compatibility reader returns only the records for non-safety callers. Results
are cached for ~5s; pass `max_age=0.0` for fresh protection evidence.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..config import cfg
from ..models import InventoryIssue, SessionProc

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


def invalidate_cache() -> None:
    """Drop the cached read (next call re-scans disk)."""
    global _sessions_cache
    _sessions_cache = None


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


def host_pid_for_sid(
    sid: str, session_procs: Sequence[SessionProc]
) -> tuple[int | None, bool]:
    """Join a sid to its host pid via the registry session files — PURE.

    Prefers a proc-alive match (`proc_alive is True` — so `alive=True` is
    trustworthy and defeats pid reuse); falls back to the first sid match
    with `alive=False`. An uninjected row (`proc_alive is None`) can never
    yield `alive=True` — only `liveness.live_session_procs` injection can.
    Returns `(None, False)` when no sessions file references the sid.

    The single host-pid join behind `cleanup.remove_session`'s M3 guard.
    """
    procs = [sp for sp in session_procs if sp.sid == sid]
    if not procs:
        return None, False
    for sp in procs:
        if sp.proc_alive is True:
            return sp.pid, True
    return procs[0].pid, False
