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
from collections.abc import Iterable, Mapping
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


@dataclass(frozen=True)
class ProcCli:
    """One agent-CLI process found by the argv walk (ADR-0005).

    `starttime` is the kernel starttime captured AT SCAN TIME so a later
    `take_over_result(pid, starttime)` recheck defeats pid reuse exactly like
    the registry `procStart` does for Claude sessions. `cwd` is best-effort
    (`readlink /proc/<pid>/cwd`) and feeds ONLY the unbound-live hint for
    bare TUIs; argv-exact matching still never guesses by directory, and an
    unreadable cwd stays "" silently — it must not degrade the walk (an
    inventory issue would disable execution-time takeovers, R10-style).
    `comm`/`exe` (C1) are equally best-effort identity annotations
    (`/proc/<pid>/comm`, `readlink /proc/<pid>/exe`) for the per-provider
    process-identity predicates: kimi rewrites its own argv at runtime, so
    cmdline alone cannot identify its processes. An unreadable comm/exe
    stays "" silently for the same reason as cwd — that record merely loses
    those identity alternatives.
    `env` (ADR-0008) holds ONLY the CLI-home variables the scan asked for
    (never the whole block, which carries secrets): it tells providers which
    state home a process is actually using, the one thing argv cannot show
    when two identities share a binary. `None` means no evidence — the keys
    were not requested, or the environ block was unreadable — and is
    deliberately distinct from `{}` ("read it; the variable is unset").
    """

    pid: int
    argv: tuple[str, ...]
    starttime: str
    cwd: str = ""
    comm: str = ""
    exe: str = ""
    env: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ProcCliInventory:
    """Known agent-CLI processes plus `/proc` walk completeness."""

    records: tuple[ProcCli, ...] = ()
    issues: tuple[ProcIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ProcOpenFileInventory:
    """Open-file targets held by selected pids, with typed `/proc` failures."""

    paths: frozenset[str] = frozenset()
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


def scan_open_file_inventory(pids: Iterable[int]) -> ProcOpenFileInventory:
    """Read exact fd symlink targets for selected live processes.

    Process/fd disappearance is a normal race and contributes no issue. An
    unreadable fd directory or link is evidence loss and stays visible so a
    caller cannot turn "unknown hosted state" into permission to act.
    """

    if not has_proc():
        return ProcOpenFileInventory(
            issues=(ProcIssue("process open files", _PROC, f"{_PROC} is unavailable"),)
        )
    paths: set[str] = set()
    issues: list[ProcIssue] = []
    for pid in sorted(set(pids)):
        fd_root = f"{_PROC}/{pid}/fd"
        try:
            entries = os.listdir(fd_root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                continue
            issues.append(ProcIssue("process open files", fd_root, str(exc)))
            continue
        for entry in entries:
            path = f"{fd_root}/{entry}"
            target, issue, disappeared = _read_inventory_link(path)
            if disappeared:
                continue
            if issue is not None:
                issues.append(ProcIssue("process open files", path, issue.detail))
                continue
            if target is not None:
                paths.add(target)
    return ProcOpenFileInventory(frozenset(paths), tuple(issues))


def ancestor_pids() -> set[int]:
    """csctl's own ancestor pid chain (including self).

    A session whose pid is in this set is the "current" one (it launched
    csctl) and is protected. Linux/WSL only — returns just `{getpid()}` when
    `/proc` is unavailable, in which case current can't be determined and
    callers must degrade (see R10).
    """
    return set(probe_current_ancestors().pids)


# --- agent-CLI argv discovery (ADR-0005) ------------------------------------
# The one /proc walk for non-Claude provider liveness. Matching is on the argv
# SHAPE, not on `comm` (a node-launched CLI can have comm `node`).


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
        return None, _cli_issue(path, str(exc)), False
    except UnicodeError as exc:
        return None, _cli_issue(path, f"malformed text: {exc}"), False


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
        return None, _cli_issue(path, str(exc)), False


def _read_inventory_env(
    path: str,
    keys: frozenset[str],
) -> dict[str, str] | None:
    """The requested environment variables of one process, or None when the
    environ block could not be read at all.

    ONLY `keys` are retained: `/proc/<pid>/environ` carries API tokens and
    other secrets wholesale, and csctl needs exactly the CLI-home variables
    that identify which state home a process is using (ADR-0008). None is
    meaningful — "no evidence either way" is different from "read it, the
    variable is unset" — so consumers can keep their pre-environ behavior
    instead of treating an unreadable block as a negative answer. Failure is
    silent for the same reason `cwd`/`comm`/`exe` are: the argv record must
    survive, and an inventory issue here would disable execution-time
    takeovers R10-style.
    """
    if not keys:
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    found: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        name, sep, value = chunk.partition(b"=")
        if not sep:
            continue
        try:
            decoded = name.decode()
        except UnicodeError:
            continue
        if decoded not in keys:
            continue
        found[decoded] = value.decode(errors="replace")
    return found


def _cli_issue(path: str, detail: str) -> ProcIssue:
    return ProcIssue("CLI process inventory", path, detail)


def scan_cli_argv_inventory(
    basenames: frozenset[str],
    env_keys: frozenset[str] = frozenset(),
) -> ProcCliInventory:
    """Walk `/proc` once for every process whose argv0 basename is in
    `basenames`, capturing argv + starttime + cwd (ADR-0005 argv-exact
    liveness for non-Claude providers).

    One walk serves ALL non-Claude providers per generation — callers pass
    the union of CLI basenames and match argv shapes themselves (pure
    per-provider extractors). `env_keys` is the union of the environment
    variables those providers need to tell their state homes apart
    (ADR-0008); only matched processes are read, and only those keys are
    kept. Degrades to an issue off Linux; per-pid disappearance races are
    ignored (a vanished pid is not evidence loss).
    """
    if not has_proc():
        return ProcCliInventory(
            issues=(_cli_issue(_PROC, f"{_PROC} is unavailable"),),
        )
    records: list[ProcCli] = []
    issues: list[ProcIssue] = []
    try:
        entries = os.listdir(_PROC)
    except OSError as exc:
        return ProcCliInventory(issues=(_cli_issue(_PROC, str(exc)),))
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
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
        if not argv or os.path.basename(argv[0]) not in basenames:
            continue
        stat = read_proc_stat(pid)
        if stat.state is ProcReadState.GONE:
            continue
        if stat.state is not ProcReadState.AVAILABLE or stat.starttime is None:
            stat_issue = stat.issue
            detail = stat_issue.detail if stat_issue else "missing starttime"
            issues.append(_cli_issue(stat.path, detail))
            continue
        # Best-effort cwd/comm/exe annotations: ANY read failure
        # (disappearance race, permissions, odd procfs) silently leaves ""
        # — the argv record itself must survive so bound liveness never
        # depends on them, and no issue is emitted (see ProcCli).
        cwd, _cwd_issue, _gone = _read_inventory_link(f"{_PROC}/{pid}/cwd")
        comm, _comm_issue, _gone = _read_inventory_text(f"{_PROC}/{pid}/comm")
        exe, _exe_issue, _gone = _read_inventory_link(f"{_PROC}/{pid}/exe")
        env = _read_inventory_env(f"{_PROC}/{pid}/environ", env_keys)
        records.append(
            ProcCli(
                pid=pid,
                argv=tuple(argv),
                starttime=stat.starttime,
                cwd=cwd or "",
                comm=(comm or "").strip(),
                exe=exe or "",
                env=env,
            )
        )
    return ProcCliInventory(tuple(records), tuple(issues))
