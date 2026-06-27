"""The only module that touches `/proc` — Linux/WSL liveness primitives.

Everything here degrades safely off Linux (no `/proc`): `proc_starttime`
returns None, `pid_alive` returns False, and `ancestor_pids` returns just this
process. Callers use `has_proc()` to detect the degraded mode and (in later
phases) refuse destructive ops when the "current" session can't be determined.
"""

from __future__ import annotations

import os

_PROC = "/proc"


def has_proc() -> bool:
    """True if `/proc` is readable (Linux/WSL). Liveness degrades when False."""
    return os.path.isdir(_PROC)


def current_determinable() -> bool:
    """Whether the "current" (csctl-launching) session can be determined.

    Needs `/proc` to walk the ancestor pid chain. When False (e.g. macOS), we
    cannot tell which session launched csctl, so callers MUST refuse destructive
    ops — terminate/delete/cleanup could otherwise hit the launching session
    (R10). This is the single predicate the data/action layers gate on.
    """
    return has_proc()


def proc_starttime(pid: int) -> str | None:
    """Field 22 (starttime) from `/proc/<pid>/stat`, or None if unavailable.

    The comm field (field 2) is wrapped in parens and may itself contain spaces
    or parens, so we slice AFTER the last ')' before splitting — a naive
    `split()[21]` would break on such names. Returns the raw string so it can be
    compared directly against the `procStart` string in `sessions/<pid>.json`.
    """
    if not has_proc():
        return None
    try:
        with open(f"{_PROC}/{pid}/stat") as fh:
            data = fh.read()
    except Exception:
        return None
    try:
        # `after` begins at field 3 (state); field 22 is at index 22 - 3.
        after = data[data.rfind(")") + 2:]
        return after.split()[22 - 3]
    except Exception:
        return None


def pid_alive(pid: int | None, proc_start: str | None) -> bool:
    """True iff `/proc/<pid>` exists AND its starttime matches `proc_start`.

    The starttime match defeats pid reuse (a recycled pid has a newer
    starttime). When `proc_start` is unknown we fall back to mere existence.
    Always False on non-Linux / missing `/proc`, so liveness degrades.
    """
    if not pid:
        return False
    st = proc_starttime(pid)
    if st is None:
        return False
    if not proc_start:
        return True
    return st == proc_start


def ancestor_pids() -> set[int]:
    """csctl's own ancestor pid chain (including self).

    A session whose pid is in this set is the "current" one (it launched
    csctl) and is protected. Linux/WSL only — returns just `{getpid()}` when
    `/proc` is unavailable, in which case current can't be determined and
    callers must degrade (see R10).
    """
    pids = {os.getpid()}
    if not has_proc():
        return pids
    pid = os.getpid()
    for _ in range(40):
        try:
            with open(f"{_PROC}/{pid}/stat") as fh:
                data = fh.read()
            ppid = int(data[data.rfind(")") + 2:].split()[1])
        except Exception:
            break
        if ppid <= 1:
            break
        pids.add(ppid)
        pid = ppid
    return pids
