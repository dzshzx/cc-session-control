"""Generic tmux adapter — THE single tmux seam.

`_tmux_run_result` owns ordinary invocations; `_capture_pane_bytes` owns bounded
streaming capture. Read/spawn wrappers keep the empty/False/None failure contract;
kill wrappers expose typed missing-target vs external-failure outcomes. Add new
tmux operations here, never as raw `subprocess` calls elsewhere.

Bottom of the `data/` DAG: this module may import only `proc` from this
package (plus stdlib) — it knows nothing about RC servers or sessions. `rc.py`
consumes it for spawning/discovering managed RC-server windows;
`actions/session_ops.py` and `actions/agent_ops.py` consume it directly for
resume/relaunch tmux windows, deliberately without depending on `rc` (CONTEXT.md:
don't conflate tmux windows with Remote Control).
"""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import NamedTuple

from . import proc


@dataclass(frozen=True)
class _TmuxInvocation:
    completed: subprocess.CompletedProcess[str] | None
    detail: str = ""


def _tmux_run_result(args: list[str]) -> _TmuxInvocation:
    """Run one tmux command while retaining expected invocation failures."""
    try:
        completed = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        return _TmuxInvocation(None, f"tmux timed out after {exc.timeout} seconds")
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        return _TmuxInvocation(None, detail)
    detail = (
        (completed.stderr or "").strip()
        or (completed.stdout or "").strip()
        or (
            f"tmux exited with status {completed.returncode}"
            if completed.returncode
            else ""
        )
    )
    return _TmuxInvocation(completed, detail)


def _tmux_run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Compatibility result-only view for non-diagnostic tmux operations."""
    return _tmux_run_result(args).completed


class TmuxWindow(NamedTuple):
    """One window of a session, with its identity metadata.

    `wid` is tmux's server-unique `@N` window id — THE collision-safe address
    for kill/capture (window NAMES are cosmetic and may collide; tmux `-t` by
    name falls back to prefix matching, which can hit the wrong window).
    `path` is the project directory the window belongs to: the `@csctl_path`
    window option when csctl declared it at spawn, else `pane_current_path`
    (adopts pre-0.7.3 windows and hand-made ones). `pid` is the pane root pid
    (the hosted process itself — spawns use `exec`, replacing the shell).
    """

    wid: str
    name: str
    dead: bool
    pid: int | None
    path: str


@dataclass(frozen=True)
class TmuxIssue:
    """One expected tmux inventory failure."""

    source: str
    detail: str


@dataclass(frozen=True)
class WindowInventory:
    """Known windows plus whether tmux proved the inventory complete."""

    records: tuple[TmuxWindow, ...] = ()
    issues: tuple[TmuxIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


_WINDOWS_FMT = (
    "#{window_id}\t#{window_name}\t#{pane_dead}\t#{pane_pid}"
    "\t#{@csctl_path}\t#{pane_current_path}"
)
_CAPTURE_HISTORY_LINES = 2_000
_CAPTURE_START = f"-{_CAPTURE_HISTORY_LINES}"
_CAPTURE_BYTE_LIMIT = 1_048_576
_CAPTURE_READ_SIZE = 64 * 1_024
_TMUX_TIMEOUT_SECONDS = 5.0
_REAP_GRACE_SECONDS = 0.2


def list_windows_inventory(session: str) -> WindowInventory:
    """Typed window inventory retaining external and malformed evidence.

    ONE tmux round-trip feeds both project↔window joins (`rc.scan`) and
    managed-server classification (`rc.scan_servers`)."""
    invocation = _tmux_run_result(["list-windows", "-t", session, "-F", _WINDOWS_FMT])
    cp = invocation.completed
    if cp is None:
        return WindowInventory(
            issues=(TmuxIssue("tmux list-windows", invocation.detail),),
        )
    if cp.returncode != 0:
        if _target_not_found(invocation.detail):
            return WindowInventory()
        return WindowInventory(
            issues=(TmuxIssue("tmux list-windows", invocation.detail),),
        )
    out: list[TmuxWindow] = []
    issues: list[TmuxIssue] = []
    for line_number, line in enumerate(cp.stdout.splitlines(), start=1):
        parts = line.split("\t", 5)
        if len(parts) != 6 or not parts[0] or parts[2] not in {"0", "1"}:
            issues.append(
                TmuxIssue(
                    "tmux list-windows",
                    f"malformed row {line_number}: {line!r}",
                )
            )
            continue
        try:
            pid = int(parts[3])
        except ValueError:
            issues.append(
                TmuxIssue(
                    "tmux list-windows",
                    f"malformed row {line_number}: invalid pane pid {parts[3]!r}",
                )
            )
            continue
        out.append(
            TmuxWindow(
                wid=parts[0],
                name=parts[1],
                dead=parts[2] == "1",
                pid=pid,
                path=parts[4] or parts[5],
            )
        )
    return WindowInventory(tuple(out), tuple(issues))


def list_windows_meta(session: str) -> list[TmuxWindow]:
    """Compatibility records-only view of :func:`list_windows_inventory`."""

    return list(list_windows_inventory(session).records)


def set_window_option(target: str, option: str, value: str) -> bool:
    """Set a per-window (user) option, e.g. `@csctl_path`; False on failure."""
    cp = _tmux_run(["set-option", "-w", "-t", target, option, value])
    return cp is not None and cp.returncode == 0


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Stop a bounded capture and always consume its child process state."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=_REAP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    process.wait()


def _capture_pane_bytes(args: list[str]) -> bytearray | None:
    """Read tmux output concurrently, never accumulating beyond the stdout cap."""
    try:
        selector = selectors.DefaultSelector()
    except OSError:
        return None
    try:
        process = subprocess.Popen(
            ["tmux", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError):
        selector.close()
        return None

    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:
        _terminate_and_reap(process)
        selector.close()
        return None

    output = bytearray()
    deadline = time.monotonic() + _TMUX_TIMEOUT_SECONDS
    hit_limit = False
    failed = False
    try:
        selector.register(stdout, selectors.EVENT_READ, "stdout")
        selector.register(stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                failed = True
                break
            events = selector.select(timeout)
            if not events:
                failed = True
                break
            for key, _mask in events:
                if key.data == "stdout":
                    remaining = _CAPTURE_BYTE_LIMIT - len(output)
                    chunk = os.read(key.fd, min(_CAPTURE_READ_SIZE, remaining))
                    if chunk:
                        output.extend(chunk)
                        hit_limit = len(output) == _CAPTURE_BYTE_LIMIT
                    else:
                        selector.unregister(key.fileobj)
                else:  # Drain stderr without retaining it or blocking its producer.
                    chunk = os.read(key.fd, _CAPTURE_READ_SIZE)
                    if not chunk:
                        selector.unregister(key.fileobj)
                if hit_limit:
                    break
            if hit_limit:
                break
    except OSError:
        failed = True
    finally:
        selector.close()

    if failed or hit_limit:
        _terminate_and_reap(process)
        stdout.close()
        stderr.close()
        return output if hit_limit and not failed else None

    try:
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate_and_reap(process)
        return None
    finally:
        stdout.close()
        stderr.close()
    return output if returncode == 0 else None


def capture_pane(target: str) -> str:
    """Recent scrollback of a tmux pane as bounded text; "" on failure.

    tmux documents negative `-S` values as history lines and `-E -` as the end
    of the visible pane. The 2,000-line range covers tmux's default history
    limit without reading the entirety of a larger user-configured history.
    Retaining the first 1,048,576 bytes favors the RC environment id
    printed near server startup.
    """
    captured = _capture_pane_bytes(
        ["capture-pane", "-p", "-S", _CAPTURE_START, "-E", "-", "-t", target]
    )
    if captured is None:
        return ""
    bounded_text = captured.decode("utf-8", errors="ignore")
    return "".join(bounded_text.splitlines(keepends=True)[:_CAPTURE_HISTORY_LINES])


def _tmux_has_session(session: str) -> bool:
    cp = _tmux_run(["has-session", "-t", session])
    return cp is not None and cp.returncode == 0


# -P -F makes tmux print the exact target of the window it just created, so
# callers enter THAT window even when names collide (no select-by-name guess).
_TARGET_FMT = "#{session_name}:#{window_index}"


def _spawned_target(cp: subprocess.CompletedProcess | None) -> str | None:
    if cp is None or cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


def _tmux_new_window(session: str, name: str, cmd: str) -> str | None:
    """Create a window; return its exact "session:index" target, or None."""
    cp = _tmux_run(
        ["new-window", "-P", "-F", _TARGET_FMT, "-t", session, "-n", name, cmd]
    )
    return _spawned_target(cp)


def _tmux_new_session(session: str, name: str, cmd: str) -> str | None:
    """Create a detached session; return its window's target, or None."""
    cp = _tmux_run(
        ["new-session", "-d", "-P", "-F", _TARGET_FMT, "-s", session, "-n", name, cmd]
    )
    return _spawned_target(cp)


class KillState(Enum):
    """Observable outcome of killing one tmux target."""

    KILLED = "killed"
    TARGET_NOT_FOUND = "target-not-found"
    FAILED = "failed"


@dataclass(frozen=True)
class KillResult:
    state: KillState
    target: str
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is KillState.KILLED


_MISSING_TARGET_PREFIXES = (
    "can't find window:",
    "can't find session:",
    "no server running on ",
)


def _target_not_found(detail: str) -> bool:
    lowered = detail.lower()
    return lowered.startswith(_MISSING_TARGET_PREFIXES) or (
        lowered.startswith("error connecting to ")
        and lowered.endswith("(no such file or directory)")
    )


def _kill_result(args: list[str], target: str) -> KillResult:
    invocation = _tmux_run_result(args)
    cp = invocation.completed
    if cp is None:
        return KillResult(KillState.FAILED, target, invocation.detail)
    if cp.returncode == 0:
        return KillResult(KillState.KILLED, target)
    detail = invocation.detail
    if _target_not_found(detail):
        return KillResult(KillState.TARGET_NOT_FOUND, target, detail)
    return KillResult(KillState.FAILED, target, detail)


def kill_window_result(target: str) -> KillResult:
    """Kill one window while distinguishing a vanished target from failure."""
    return _kill_result(["kill-window", "-t", target], target)


def kill_window(target: str) -> bool:
    """Compatibility bool view of :func:`kill_window_result`."""
    return kill_window_result(target).success


def kill_session_result(session: str) -> KillResult:
    """Kill one session while distinguishing absence from external failure."""
    return _kill_result(["kill-session", "-t", session], session)


def kill_session(session: str) -> bool:
    """Compatibility bool view of :func:`kill_session_result`."""
    return kill_session_result(session).success


def session_name_for(cwd: str) -> str:
    """tmux session name for a project directory: its basename, with the tmux
    target separators `.`/`:` (illegal in session names) replaced by `-`.

    One session per project — the grouping rule shared by every claude spawn
    (t 新建 / t 接回 / R 转入后台 / agents respawn). Empty cwd → "claude"."""
    base = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    name = base.replace(".", "-").replace(":", "-").strip()
    return name or "claude"


def run_in_tmux(session: str, window: str, cmd: str) -> str | None:
    """Run `cmd` in a tmux `window` under `session`, creating the session if
    it doesn't exist yet. Returns the exact "session:window_index" target of
    the new window (None on failure) so callers can enter it unambiguously.
    Public seam for relaunching a session outside the managed RC server
    machinery."""
    if _tmux_has_session(session):
        return _tmux_new_window(session, window, cmd)
    return _tmux_new_session(session, window, cmd)


class TmuxPane(NamedTuple):
    """One pane root pid and its enterable session/window target."""

    target: str
    pid: int


@dataclass(frozen=True)
class ResidencyIssue:
    """One source preventing a complete tmux-residency inventory."""

    source: str
    path: str | None
    detail: str


@dataclass(frozen=True)
class PaneInventory:
    """Known panes plus whether tmux proved the global list complete."""

    records: tuple[TmuxPane, ...] = ()
    issues: tuple[ResidencyIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ResidencyInventory:
    """Known pid→target joins plus completeness across tmux and `/proc`."""

    targets: Mapping[int, str] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    issues: tuple[ResidencyIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def complete(self) -> bool:
        return not self.issues


def list_panes_inventory() -> PaneInventory:
    """Typed global pane inventory for residency safety decisions."""

    invocation = _tmux_run_result(
        ["list-panes", "-a", "-F", "#{session_name}:#{window_index}\t#{pane_pid}"]
    )
    cp = invocation.completed
    if cp is None:
        return PaneInventory(
            issues=(ResidencyIssue("tmux list-panes", None, invocation.detail),),
        )
    if cp.returncode != 0:
        if _target_not_found(invocation.detail):
            return PaneInventory()
        return PaneInventory(
            issues=(ResidencyIssue("tmux list-panes", None, invocation.detail),),
        )
    out: list[TmuxPane] = []
    issues: list[ResidencyIssue] = []
    for line_number, line in enumerate(cp.stdout.splitlines(), start=1):
        target, _, pid_s = line.partition("\t")
        try:
            pid = int(pid_s.strip())
        except ValueError:
            issues.append(
                ResidencyIssue(
                    "tmux list-panes",
                    None,
                    f"malformed row {line_number}: {line!r}",
                )
            )
            continue
        if not target.strip() or pid <= 0:
            issues.append(
                ResidencyIssue(
                    "tmux list-panes",
                    None,
                    f"malformed row {line_number}: {line!r}",
                )
            )
            continue
        out.append(TmuxPane(target.strip(), pid))
    return PaneInventory(tuple(out), tuple(issues))


def _tmux_list_all_panes() -> list[tuple[str, int]]:
    """Compatibility records-only view of :func:`list_panes_inventory`."""

    return list(list_panes_inventory().records)


def window_containing(
    panes: Sequence[tuple[str, int]],
    ancestors: set[int],
) -> str | None:
    """PURE: the pane target whose pane_pid appears in `ancestors`, or None.

    A session process hosted by a tmux pane has that pane's pid in its
    ancestor chain (the pane pid is the window's root process)."""
    for target, pane_pid in panes:
        if pane_pid in ancestors:
            return target
    return None


def residency_inventory(pids: Iterable[int]) -> ResidencyInventory:
    """Typed pid→pane join retaining tmux and ancestor-scan uncertainty.

    THE batch tmux-residency computation (ADR-0001 badge + actions share it):
    ONE `list-panes -a` for the whole pid set, then each pid's `/proc` ancestor
    chain is matched against the pane root pids — finds windows in ANY tmux
    session (per-project, rc, user-made)."""
    pid_list = list(pids)
    if not pid_list:
        return ResidencyInventory()
    pane_inventory = list_panes_inventory()
    panes = list(pane_inventory.records)
    issues = list(pane_inventory.issues)
    out: dict[int, str] = {}
    for pid in pid_list:
        ancestors = proc.probe_ancestors(pid)
        issues.extend(
            ResidencyIssue(issue.source, issue.path, issue.detail)
            for issue in ancestors.issues
        )
        target = window_containing(panes, set(ancestors.pids))
        if target:
            out[pid] = target
    return ResidencyInventory(out, tuple(issues))


def residency_targets(pids: Iterable[int]) -> dict[int, str]:
    """Compatibility records-only view of :func:`residency_inventory`."""

    return dict(residency_inventory(pids).targets)


@dataclass(frozen=True)
class SessionWindowResult:
    """First matching pane target plus evidence completeness."""

    target: str | None = None
    issues: tuple[ResidencyIssue, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.issues


def find_session_window_result(pids: list[int]) -> SessionWindowResult:
    """Typed first-target convenience over :func:`residency_inventory`."""

    inventory = residency_inventory(pids)
    for pid in pids:
        if pid in inventory.targets:
            return SessionWindowResult(inventory.targets[pid], inventory.issues)
    return SessionWindowResult(issues=inventory.issues)


def find_session_window(pids: list[int]) -> str | None:
    """Compatibility target-only view of :func:`find_session_window_result`."""

    return find_session_window_result(pids).target


def select_window(target: str) -> bool:
    """tmux select-window -t <target>; False on failure (non-fatal for attach)."""
    cp = _tmux_run(["select-window", "-t", target])
    return cp is not None and cp.returncode == 0


def switch_client(target: str) -> bool:
    """tmux switch-client -t <target> — the inside-tmux attach equivalent."""
    cp = _tmux_run(["switch-client", "-t", target])
    return cp is not None and cp.returncode == 0
