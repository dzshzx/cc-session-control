"""The only module that touches `/proc` — Linux/WSL liveness primitives.

Everything here degrades safely off Linux (no `/proc`): `proc_starttime`
returns None, `pid_alive` returns False, and `ancestor_pids` returns just this
process. Callers use `has_proc()` to detect the degraded mode and (in later
phases) refuse destructive ops when the "current" session can't be determined.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

_PROC = "/proc"


@dataclass
class ProcRC:
    """A /proc-discovered Claude project RC server (`claude remote-control`).

    Internal to this module — the public, view-facing model is `RCServer`
    (assembled in `data/rc.py` after classifying managed vs external). `pid` is
    0 when produced by the pure matcher (the scanner fills it); `cwd` comes from
    `readlink(/proc/<pid>/cwd)`.
    """
    pid: int
    name: str = ""
    cwd: str = ""


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


# --- project RC server discovery (R5 / D5) ---------------------------------
# A real `claude remote-control --name <name>` server's /proc cmdline shows the
# FULL argv (verified live: a bare interactive `claude` instead collapses its
# cmdline to just `claude`), so we match on the argv SHAPE, not on `comm` (a
# node-launched claude can have comm `node`). Other tools are excluded — codex
# runs `--remote-control` as a FLAG with argv0 `codex` and no `remote-control`
# subcommand token, so it never matches.


def _split_cmdline(cmdline: str) -> list[str]:
    """Split a `/proc/<pid>/cmdline` string into argv.

    Real cmdlines are NUL-separated (with a trailing NUL). A space-joined string
    (test convenience / odd launchers) is tolerated by falling back to a shell
    split when no NUL boundaries are present.
    """
    parts = [p for p in cmdline.split("\0") if p]
    if len(parts) <= 1 and cmdline.strip() and " " in cmdline.strip():
        try:
            parts = shlex.split(cmdline)
        except ValueError:
            parts = cmdline.split()
    return parts


def _flag_value(argv: list[str], flag: str) -> str | None:
    """Value of `--flag value` or `--flag=value` in argv; None if absent/empty."""
    prefix = flag + "="
    for i, tok in enumerate(argv):
        if tok == flag:
            return argv[i + 1] if i + 1 < len(argv) else None
        if tok.startswith(prefix):
            return tok[len(prefix):] or None
    return None


def _match_rc_cmdline(comm: str, cmdline: str) -> ProcRC | None:
    """PURE matcher (no IO): is this argv a Claude project RC server? (AC5)

    Matches iff the program basename is `claude` AND a bare `remote-control`
    subcommand token is present AND a `--name <name>` flag is parseable. `comm`
    is accepted for signature completeness but deliberately NOT trusted on its
    own. Returns a `ProcRC` (pid=0, filled by the scanner) or None.
    """
    argv = _split_cmdline(cmdline)
    if not argv:
        return None
    if os.path.basename(argv[0]) != "claude":
        return None
    if "remote-control" not in argv[1:]:
        return None
    name = _flag_value(argv, "--name")
    if not name:
        return None
    return ProcRC(pid=0, name=name)


def _read_text(path: str) -> str:
    try:
        with open(path, errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def _read_link(path: str) -> str:
    try:
        return os.readlink(path)
    except Exception:
        return ""


def scan_rc_servers() -> list[ProcRC]:
    """Walk `/proc` for Claude project RC server processes (R5).

    Reads each pid's `comm` + `cmdline`, runs the pure `_match_rc_cmdline`, and
    fills `pid` + `cwd` (`readlink /proc/<pid>/cwd`) for matches. Degrades to
    `[]` off Linux (no `/proc`) and swallows all per-pid errors.
    """
    if not has_proc():
        return []
    servers: list[ProcRC] = []
    try:
        entries = os.listdir(_PROC)
    except Exception:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            pid = int(entry)
            comm = _read_text(f"{_PROC}/{pid}/comm").strip()
            cmdline = _read_text(f"{_PROC}/{pid}/cmdline")
            match = _match_rc_cmdline(comm, cmdline)
            if match is None:
                continue
            cwd = _read_link(f"{_PROC}/{pid}/cwd")
            servers.append(ProcRC(pid=pid, name=match.name, cwd=cwd or match.cwd))
        except Exception:
            continue
    return servers
