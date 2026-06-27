"""Session liveness — the single authority.

Owns the ONE module-global cache for `claude agents --json` (mirrored by the
`agents.py` re-export shim so terminate's invalidate and scan's read share it).
`live_index` is a pure merge of already-fetched liveness inputs (registry
session files with injected proc liveness + `claude agents --json`).
"""

from __future__ import annotations

import json
import subprocess
import time

from ..models import LiveInfo, SessionProc

_cache: dict[str, int | None] | None = None
_cache_time: float = 0.0


def alive_map(max_age: float = 5.0) -> dict[str, int | None]:
    """Return {session_id: pid} for all known agents. Cached for max_age seconds."""
    global _cache, _cache_time
    now = time.monotonic()
    if _cache is not None and (now - _cache_time) < max_age:
        return _cache
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        result = {
            a.get("sessionId"): a.get("pid")
            for a in json.loads(out or "[]")
            if a.get("sessionId")
        }
    except Exception:
        result = {}
    _cache = result
    _cache_time = now
    return result


def invalidate_cache() -> None:
    global _cache
    _cache = None


def _source_of(entrypoint: str, kind: str) -> str:
    """Coarse source bucket from the registry entrypoint/kind (D9)."""
    if kind == "bg":
        return "bg"
    if entrypoint == "claude-vscode":
        return "vscode"
    if entrypoint == "sdk-ts":
        return "sdk"
    return "cli"


def _is_rc_exposed(bridge: str | None, pid_alive: bool) -> bool:
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
    session_procs: list[SessionProc],
    agents_map: dict[str, int | None],
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
            pids = [p.pid for p in alive_procs]
        else:
            chosen = max(procs, key=lambda p: _start_key(p.proc_start))
            alive = False
            pids = []
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
    # sids and rescues the degraded (no-/proc) path.
    for sid, pid in agents_map.items():
        if not sid:
            continue
        info = index.get(sid)
        if info is None:
            index[sid] = LiveInfo(
                sid=sid, pid=pid, alive=True, pids=[pid] if pid else []
            )
            continue
        info.alive = True
        if info.pid is None:
            info.pid = pid
        if pid and pid not in info.pids:
            info.pids.append(pid)
    return index
