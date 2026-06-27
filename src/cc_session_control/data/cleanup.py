"""Cleanup strategies for Claude Code's on-disk state (D6/R7).

Two strategies, both preview-first (a `list_*`/`*_stats` read split from the
matching `remove_*` write, so the view can preview then confirm):

- **Strategy A — key-typed orphan sweep.** Key semantics are PER DIRECTORY,
  never a blanket `uuid == sessionId` rule:
    * sid-keyed dirs (`session-env`, `file-history`, `tasks`, `uploads`):
      orphan = an entry whose name (a sessionId) is not in the known sid set.
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
destructive ops here refuse (return empty/no-op) rather than fail open — without
`/proc` every pid looks dead, so a zombie sweep would delete the live/current
session's files. Strategy B is mtime-only and session-agnostic, so it is not
gated on `/proc`.
"""

from __future__ import annotations

import os
import shutil
import time

from ..config import cfg
from ..models import Session, SessionProc
from . import proc

# Dirs keyed by full sessionId — orphan = name not in the known sid set.
_SID_DIRS = ("session_env", "file_history", "tasks", "uploads")
# Dirs swept purely by mtime (not session-keyed).
_AGE_DIRS = ("shell_snapshots", "telemetry", "plans", "backups", "paste_cache")

_SECONDS_PER_DAY = 86400


def _sid_dir_paths() -> list[tuple[str, str]]:
    """(label, path) for each sid-keyed directory, via cfg props only."""
    return [(name.replace("_", "-"), str(getattr(cfg, f"{name}_dir")))
            for name in _SID_DIRS]


def _age_dir_paths() -> list[tuple[str, str]]:
    """(label, path) for each age-swept directory, via cfg props only."""
    return [(name.replace("_", "-"), str(getattr(cfg, f"{name}_dir")))
            for name in _AGE_DIRS]


def _session_artifact_paths(sid: str) -> list[str]:
    """All on-disk artifact paths owned by one session id (cfg-derived).

    Covers the sid-keyed dirs plus the 8-char-prefixed `jobs/<short>` dir for
    this session. Used by `remove_session` (a full delete of one session), not
    by the orphan sweep.
    """
    paths = [os.path.join(p, sid) for _, p in _sid_dir_paths()]
    paths.append(os.path.join(str(cfg.jobs_dir), sid[:8]))
    return paths


def _remove_path(path: str) -> bool:
    """Remove a file or directory; True iff something was removed."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
        return True
    if os.path.isfile(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False
    return False


# --- Strategy A: sid-keyed orphan dirs -------------------------------------

def list_orphan_dirs(sessions: list[Session]) -> list[str]:
    """Orphan sid-keyed artifact entries (`<dir>/<sid>`), preview list.

    Refuses (returns []) when current can't be determined (R10).
    """
    if not proc.current_determinable():
        return []
    known = {s.sid for s in sessions}
    orphans: list[str] = []
    for label, path in _sid_dir_paths():
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            if name not in known:
                orphans.append(os.path.join(label, name))
    return sorted(set(orphans))


def remove_orphan_dirs(sessions: list[Session]) -> int:
    """Delete orphan sid-keyed artifact entries. Refuses without `/proc`."""
    if not proc.current_determinable():
        return 0
    known = {s.sid for s in sessions}
    count = 0
    for _label, path in _sid_dir_paths():
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            if name in known:
                continue
            if _remove_path(os.path.join(path, name)):
                count += 1
    return count


# --- Strategy A: pid-keyed zombie session files ----------------------------

def select_zombie_pids(session_procs: list[SessionProc], cur: set[int]) -> list[int]:
    """Removable `sessions/<pid>.json` pids — PURE (no IO), for unit tests.

    A pid file is removable iff its proc is dead (`not proc_alive`) and the pid
    is neither the current session's nor a live one. For a resumed multi-pid sid
    this returns only the dead pid(s); the live pid's file is kept because its
    injected `proc_alive` is True. Inputs are injected (proc liveness already on
    each `SessionProc`, `cur` = ancestor-pid set).
    """
    out: list[int] = []
    for sp in session_procs:
        if sp.pid in cur:        # current session's pid file — protected
            continue
        if sp.proc_alive:        # live runtime — keep (multi-pid: keep alive pid)
            continue
        out.append(sp.pid)
    return sorted(set(out))


def remove_zombie_session_files(session_procs: list[SessionProc], cur: set[int]) -> int:
    """Delete zombie `sessions/<pid>.json` files. Refuses without `/proc`."""
    if not proc.current_determinable():
        return 0
    count = 0
    for pid in select_zombie_pids(session_procs, cur):
        if _remove_path(os.path.join(str(cfg.sessions_dir), f"{pid}.json")):
            count += 1
    return count


# --- Strategy B: age sweep -------------------------------------------------

def _age_cutoff(now: float) -> float:
    return now - cfg.cleanup_age_days * _SECONDS_PER_DAY


def list_aged_entries(now: float | None = None) -> list[str]:
    """Age-swept entries (`<dir>/<name>`) older than `cfg.cleanup_age_days`."""
    cutoff = _age_cutoff(time.time() if now is None else now)
    out: list[str] = []
    for label, path in _age_dir_paths():
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            try:
                if os.stat(os.path.join(path, name)).st_mtime < cutoff:
                    out.append(os.path.join(label, name))
            except OSError:
                pass
    return sorted(out)


def remove_aged_entries(now: float | None = None) -> int:
    """Delete age-swept entries older than `cfg.cleanup_age_days`."""
    cutoff = _age_cutoff(time.time() if now is None else now)
    count = 0
    for _label, path in _age_dir_paths():
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                if os.stat(full).st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if _remove_path(full):
                count += 1
    return count


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


def remove_session(s: Session) -> None:
    """Delete one session: its `.jsonl`, companion dir, and sid artifacts.

    Refuses (no-op) when current can't be determined (R10) — without `/proc` we
    cannot prove `s` is not the launching session.
    """
    if not proc.current_determinable():
        return
    try:
        os.remove(s.file)
    except OSError:
        pass
    _remove_path(s.file[:-6])  # companion dir (drop the .jsonl suffix)
    for p in _session_artifact_paths(s.sid):
        _remove_path(p)


# --- Classified counts -----------------------------------------------------

def cleanup_stats(sessions: list[Session]) -> dict[str, int]:
    """Summary counts for the Sessions cleanup submenu (view-facing contract).

    Keeps the established 4-key shape (`total/empty/short/orphans`) that the
    view reads; `orphans` is the sid-keyed orphan count (Strategy A).
    """
    return {
        "total": len(sessions),
        "empty": sum(1 for s in sessions if s.prompts == 0),
        "short": sum(1 for s in sessions if 0 < s.prompts <= 2),
        "orphans": len(list_orphan_dirs(sessions)),
    }


def cleanup_classified(
    sessions: list[Session],
    session_procs: list[SessionProc],
    cur: set[int],
    now: float | None = None,
) -> dict[str, int]:
    """Per-category cleanup counts (D6). Deps injected so it stays unit-testable.

    Breaks the cleanup surface into its categories — empty/short sessions,
    sid-keyed orphan dirs, pid-keyed zombie session files, and age-swept global
    entries — for the (Phase 7) workbench view to surface.
    """
    return {
        "empty": sum(1 for s in sessions if s.prompts == 0),
        "short": sum(1 for s in sessions if 0 < s.prompts <= 2),
        "orphan_dirs": len(list_orphan_dirs(sessions)),
        "zombie_procs": len(select_zombie_pids(session_procs, cur)),
        "aged_entries": len(list_aged_entries(now)),
    }
