"""The only module that touches `/proc` — Linux/WSL liveness primitives.

Typed probes distinguish confirmed process disappearance from unavailable or
malformed evidence. Security-sensitive callers consume those probes directly
and refuse destructive work when liveness or ancestry is incomplete; the
legacy bool/set helpers are compatibility views only.
"""

from __future__ import annotations

import errno
import os
import shlex
from dataclasses import dataclass
from enum import StrEnum

from ..models import InventoryIssue

_PROC = "/proc"


class ProcReadState(StrEnum):
    """Outcome of reading one `/proc/<pid>/stat` record."""

    AVAILABLE = "available"
    GONE = "gone"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"


#: Same (source, path, detail) record everywhere — one canonical issue type.
ProcIssue = InventoryIssue


@dataclass(frozen=True)
class ProcStatRead:
    """Typed `/proc/<pid>/stat` read with parsed identity and ancestry fields."""

    pid: int
    state: ProcReadState
    path: str
    starttime: str | None = None
    ppid: int | None = None
    detail: str = ""

    @property
    def issue(self) -> ProcIssue | None:
        if self.state not in {
            ProcReadState.UNAVAILABLE,
            ProcReadState.MALFORMED,
        }:
            return None
        return ProcIssue("process stat", self.path, self.detail)


@dataclass(frozen=True)
class PidProbe:
    """Tri-state process liveness: alive, gone/reused, or unknown."""

    pid: int | None
    alive: bool | None
    stat: ProcStatRead | None = None
    issue: ProcIssue | None = None


@dataclass(frozen=True)
class AncestorProbe:
    """Known ancestor pids plus evidence showing whether the walk completed."""

    pids: frozenset[int]
    issues: tuple[ProcIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


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


@dataclass(frozen=True)
class ProcRCInventory:
    """Known RC server processes plus `/proc` scan completeness."""

    records: tuple[ProcRC, ...] = ()
    issues: tuple[ProcIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


def has_proc() -> bool:
    """True if `/proc` is readable (Linux/WSL). Liveness degrades when False."""
    return os.path.isdir(_PROC)


def _malformed_stat(pid: int, path: str, detail: str) -> ProcStatRead:
    return ProcStatRead(
        pid=pid,
        state=ProcReadState.MALFORMED,
        path=path,
        detail=f"malformed stat: {detail}",
    )


def read_proc_stat(pid: int) -> ProcStatRead:
    """Read identity and parent fields without collapsing uncertainty into gone."""
    path = f"{_PROC}/{pid}/stat"
    if not has_proc():
        return ProcStatRead(
            pid=pid,
            state=ProcReadState.UNAVAILABLE,
            path=path,
            detail=f"{_PROC} is unavailable",
        )
    try:
        with open(path) as fh:
            data = fh.read()
    except FileNotFoundError:
        return ProcStatRead(pid=pid, state=ProcReadState.GONE, path=path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return ProcStatRead(pid=pid, state=ProcReadState.GONE, path=path)
        return ProcStatRead(
            pid=pid,
            state=ProcReadState.UNAVAILABLE,
            path=path,
            detail=str(exc),
        )
    except UnicodeError as exc:
        return _malformed_stat(pid, path, str(exc))

    open_paren = data.find("(")
    close_paren = data.rfind(")")
    if open_paren <= 0 or close_paren <= open_paren:
        return _malformed_stat(pid, path, "missing comm delimiters")
    if data[:open_paren].strip() != str(pid):
        return _malformed_stat(pid, path, "pid field does not match path")
    fields = data[close_paren + 1 :].split()
    if len(fields) <= 22 - 3:
        return _malformed_stat(pid, path, "truncated before field 22")
    if len(fields[0]) != 1 or not fields[0].isascii() or not fields[0].isalpha():
        return _malformed_stat(pid, path, "invalid state field")
    try:
        ppid = int(fields[1])
        starttime = fields[22 - 3]
        starttime_value = int(starttime)
    except (IndexError, ValueError) as exc:
        return _malformed_stat(pid, path, str(exc))
    if ppid < 0 or starttime_value < 0:
        return _malformed_stat(pid, path, "negative ppid or starttime")
    return ProcStatRead(
        pid=pid,
        state=ProcReadState.AVAILABLE,
        path=path,
        starttime=starttime,
        ppid=ppid,
    )


def probe_pid(pid: int | None, proc_start: str | None) -> PidProbe:
    """Probe one pid while preserving unavailable or malformed evidence."""
    if not pid:
        return PidProbe(pid=pid, alive=False)
    stat = read_proc_stat(pid)
    if stat.state is ProcReadState.GONE:
        return PidProbe(pid=pid, alive=False, stat=stat)
    if stat.state is not ProcReadState.AVAILABLE:
        return PidProbe(pid=pid, alive=None, stat=stat, issue=stat.issue)
    alive = not proc_start or stat.starttime == proc_start
    return PidProbe(pid=pid, alive=alive, stat=stat)


def pid_alive(pid: int | None, proc_start: str | None) -> bool:
    """Compatibility bool view; unknown evidence is conservatively False."""
    return probe_pid(pid, proc_start).alive is True


def probe_ancestors(start_pid: int) -> AncestorProbe:
    """Walk a pid's ancestors without discarding a partial chain on failure."""
    pids = {start_pid}
    pid = start_pid
    for _ in range(40):
        stat = read_proc_stat(pid)
        if stat.state is ProcReadState.GONE:
            if pid == start_pid:
                return AncestorProbe(frozenset(pids))
            gone_issue = ProcIssue(
                "process ancestors",
                stat.path,
                "process disappeared before ancestor chain completed",
            )
            return AncestorProbe(frozenset(pids), (gone_issue,))
        if stat.state is not ProcReadState.AVAILABLE:
            stat_issue = stat.issue
            if stat_issue is None:
                raise AssertionError("incomplete stat read must carry an issue")
            return AncestorProbe(
                frozenset(pids),
                (
                    ProcIssue(
                        "process ancestors",
                        stat_issue.path,
                        stat_issue.detail,
                    ),
                ),
            )
        ppid = stat.ppid
        if ppid is None:
            raise AssertionError("available stat read must carry ppid")
        if ppid <= 1:
            return AncestorProbe(frozenset(pids))
        if ppid in pids:
            issue = ProcIssue(
                "process ancestors",
                stat.path,
                f"ancestor cycle at pid {ppid}",
            )
            return AncestorProbe(frozenset(pids), (issue,))
        pids.add(ppid)
        pid = ppid
    issue = ProcIssue(
        "process ancestors",
        f"{_PROC}/{pid}/stat",
        "ancestor chain exceeded 40 processes",
    )
    return AncestorProbe(frozenset(pids), (issue,))


def probe_current_ancestors() -> AncestorProbe:
    """Typed ancestor evidence for the csctl process."""
    return probe_ancestors(os.getpid())


def ancestor_pids() -> set[int]:
    """csctl's own ancestor pid chain (including self).

    A session whose pid is in this set is the "current" one (it launched
    csctl) and is protected. Linux/WSL only — returns just `{getpid()}` when
    `/proc` is unavailable, in which case current can't be determined and
    callers must degrade (see R10).
    """
    return set(probe_current_ancestors().pids)


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
            return tok[len(prefix) :] or None
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


def _rc_issue(path: str, detail: str) -> ProcIssue:
    return ProcIssue("RC process inventory", path, detail)


def _read_inventory_text(
    path: str,
) -> tuple[str | None, ProcIssue | None, bool]:
    """Read one per-pid text file as `(value, issue, disappeared)`."""

    try:
        with open(path) as fh:
            return fh.read(), None, False
    except FileNotFoundError:
        return None, None, True
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None, None, True
        return None, _rc_issue(path, str(exc)), False
    except UnicodeError as exc:
        return None, _rc_issue(path, f"malformed text: {exc}"), False


def _read_inventory_link(
    path: str,
) -> tuple[str | None, ProcIssue | None, bool]:
    try:
        return os.readlink(path), None, False
    except FileNotFoundError:
        return None, None, True
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None, None, True
        return None, _rc_issue(path, str(exc)), False


def scan_rc_server_inventory() -> ProcRCInventory:
    """Walk `/proc` without collapsing incomplete process evidence.

    Reads each pid's `comm` + `cmdline`, runs the pure `_match_rc_cmdline`, and
    fills `pid` + `cwd` (`readlink /proc/<pid>/cwd`) for matches. Degrades to
    an issue off Linux and ignores only confirmed per-pid disappearance races.
    """
    if not has_proc():
        return ProcRCInventory(
            issues=(_rc_issue(_PROC, f"{_PROC} is unavailable"),),
        )
    servers: list[ProcRC] = []
    issues: list[ProcIssue] = []
    try:
        entries = os.listdir(_PROC)
    except OSError as exc:
        return ProcRCInventory(issues=(_rc_issue(_PROC, str(exc)),))
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        comm_path = f"{_PROC}/{pid}/comm"
        comm, issue, disappeared = _read_inventory_text(comm_path)
        if disappeared:
            continue
        if issue is not None:
            issues.append(issue)
            continue
        if comm is None:
            raise AssertionError("available process comm must carry text")

        cmdline_path = f"{_PROC}/{pid}/cmdline"
        cmdline, issue, disappeared = _read_inventory_text(cmdline_path)
        if disappeared:
            continue
        if issue is not None:
            issues.append(issue)
            continue
        if cmdline is None:
            raise AssertionError("available process cmdline must carry text")

        argv = _split_cmdline(cmdline)
        if (
            argv
            and os.path.basename(argv[0]) == "claude"
            and "remote-control" in argv[1:]
            and _flag_value(argv, "--name") is None
        ):
            issues.append(
                _rc_issue(cmdline_path, "malformed RC argv: missing --name value")
            )
            continue
        match = _match_rc_cmdline(comm, cmdline)
        if match is None:
            continue
        cwd_path = f"{_PROC}/{pid}/cwd"
        cwd, issue, disappeared = _read_inventory_link(cwd_path)
        if disappeared:
            continue
        if issue is not None:
            issues.append(issue)
        servers.append(ProcRC(pid=pid, name=match.name, cwd=cwd or match.cwd))
    return ProcRCInventory(tuple(servers), tuple(issues))
