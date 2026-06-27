"""Read Claude Code's session/agent registries — pure parse, ~5s TTL cache.

Two on-disk registers Claude Code maintains itself:
  - `sessions/<pid>.json`  → one per local runtime (a sid can have several)
  - `jobs/<short>/state.json` → one per background agent (NO pid inside)

Both readers swallow errors and return `[]` so the TUI never crashes on a
malformed/absent register. Results are cached for ~5s (mirrors
`liveness.alive_map`) so the shared world snapshot can reuse them; pass
`max_age=0.0` to force a fresh read (used by tests that swap `cfg` paths).
"""

from __future__ import annotations

import glob
import json
import os
import time

from ..config import cfg
from ..models import AgentJob, SessionProc

_sessions_cache: list[SessionProc] | None = None
_sessions_time: float = 0.0
_jobs_cache: list[AgentJob] | None = None
_jobs_time: float = 0.0


def invalidate_cache() -> None:
    """Drop both cached reads (next call re-scans disk)."""
    global _sessions_cache, _jobs_cache
    _sessions_cache = None
    _jobs_cache = None


def _suffix(bridge: str | None) -> str:
    """Namespace suffix of a bridge id (`cse_abc` -> `abc`), or ""."""
    if not bridge or "_" not in bridge:
        return ""
    return bridge.split("_", 1)[1]


def _parse_session_proc(path: str) -> SessionProc | None:
    try:
        with open(path, errors="ignore") as fh:
            d = json.load(fh)
    except Exception:
        return None
    sid = d.get("sessionId")
    pid = d.get("pid")
    if not sid or not pid:
        return None
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    return SessionProc(
        pid=pid_int,
        sid=str(sid),
        cwd=d.get("cwd", "") or "",
        kind=d.get("kind", "") or "",
        entrypoint=d.get("entrypoint", "") or "",
        status=d.get("status", "") or "",
        proc_start=str(d.get("procStart", "") or ""),
        bridge=d.get("bridgeSessionId"),
    )


def read_session_procs(max_age: float = 5.0) -> list[SessionProc]:
    """All parseable `sessions/<pid>.json` entries. Cached for `max_age` secs."""
    global _sessions_cache, _sessions_time
    now = time.monotonic()
    if _sessions_cache is not None and (now - _sessions_time) < max_age:
        return _sessions_cache
    rows: list[SessionProc] = []
    try:
        for path in glob.glob(os.path.join(str(cfg.sessions_dir), "*.json")):
            row = _parse_session_proc(path)
            if row is not None:
                rows.append(row)
    except Exception:
        rows = []
    _sessions_cache = rows
    _sessions_time = now
    return rows


def _parse_agent_job(state_path: str) -> AgentJob | None:
    short = os.path.basename(os.path.dirname(state_path))
    if not short:
        return None
    try:
        with open(state_path, errors="ignore") as fh:
            d = json.load(fh)
    except Exception:
        return None
    sid = str(d.get("sessionId") or "")
    flags = d.get("respawnFlags")
    if not isinstance(flags, list):
        flags = []
    return AgentJob(
        short=short,
        sid=sid,
        resume_sid=str(d.get("resumeSessionId") or sid or ""),
        state=d.get("state", "") or "",
        tempo=d.get("tempo", "") or "",
        cwd=d.get("cwd", "") or "",
        name=d.get("name", "") or "",
        env_suffix=_suffix(d.get("bridgeSessionId")),
        respawn_flags=[str(x) for x in flags],
    )


def read_agent_jobs(max_age: float = 5.0) -> list[AgentJob]:
    """All parseable `jobs/<short>/state.json` records. Cached for `max_age`."""
    global _jobs_cache, _jobs_time
    now = time.monotonic()
    if _jobs_cache is not None and (now - _jobs_time) < max_age:
        return _jobs_cache
    rows: list[AgentJob] = []
    try:
        pattern = os.path.join(str(cfg.jobs_dir), "*", "state.json")
        for state_path in glob.glob(pattern):
            row = _parse_agent_job(state_path)
            if row is not None:
                rows.append(row)
    except Exception:
        rows = []
    _jobs_cache = rows
    _jobs_time = now
    return rows


def host_pid_for_sid(
    sid: str, session_procs: list[SessionProc]
) -> tuple[int | None, bool]:
    """Join a sid to its host pid via the registry session files — PURE.

    `jobs/<short>/state.json` carries NO pid, so a background/agent worker's host
    pid is the `sessions/<pid>.json` entry sharing the sid. Prefers a proc-alive
    match (so `alive=True` is trustworthy and defeats pid reuse); falls back to
    the first sid match with `alive=False`. Relies on each `SessionProc.proc_alive`
    already being injected by the caller (no IO here). Returns `(None, False)`
    when no sessions file references the sid (that live worker is unstoppable).

    The single host-pid join shared by `snapshot._enrich_jobs`,
    `actions.agent_ops.job_host`, and `cleanup.remove_session` (M3 guard).
    """
    procs = [sp for sp in session_procs if sp.sid == sid]
    if not procs:
        return None, False
    for sp in procs:
        if sp.proc_alive:
            return sp.pid, True
    return procs[0].pid, False
