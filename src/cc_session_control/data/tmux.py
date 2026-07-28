"""Generic tmux adapter — THE single tmux seam.

Only `_tmux_run_result` touches `subprocess`; `_tmux_run` is its result-only
compatibility view. Read/spawn wrappers keep the legacy swallow-errors contract
(return empty/False/None on failure), while kill wrappers expose typed missing
target vs external-failure outcomes. Add new tmux operations here, not as raw
`subprocess` calls elsewhere.

Bottom of the `data/` DAG: this module may import only `proc` from this
package (plus stdlib) — it knows nothing about RC servers or sessions. `rc.py`
consumes it for spawning/discovering managed RC-server windows;
`actions/session_ops.py` and `actions/agent_ops.py` consume it directly for
resume/relaunch tmux windows, deliberately without depending on `rc` (CONTEXT.md:
don't conflate tmux windows with Remote Control).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
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


_WINDOWS_FMT = (
    "#{window_id}\t#{window_name}\t#{pane_dead}\t#{pane_pid}"
    "\t#{@csctl_path}\t#{pane_current_path}"
)
_CAPTURE_HISTORY_LINES = 2_000
_CAPTURE_TEXT_CHAR_LIMIT = 1_048_576


def list_windows_meta(session: str) -> list[TmuxWindow]:
    """All windows of `session` with identity metadata; [] on failure.

    ONE tmux round-trip feeds both project↔window joins (`rc.scan`) and
    managed-server classification (`rc.scan_servers`)."""
    cp = _tmux_run(["list-windows", "-t", session, "-F", _WINDOWS_FMT])
    if cp is None:
        return []
    out: list[TmuxWindow] = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 6 or not parts[0]:
            continue
        try:
            pid: int | None = int(parts[3])
        except ValueError:
            pid = None
        out.append(
            TmuxWindow(
                wid=parts[0],
                name=parts[1],
                dead=parts[2] == "1",
                pid=pid,
                path=parts[4] or parts[5],
            )
        )
    return out


def set_window_option(target: str, option: str, value: str) -> bool:
    """Set a per-window (user) option, e.g. `@csctl_path`; False on failure."""
    cp = _tmux_run(["set-option", "-w", "-t", target, option, value])
    return cp is not None and cp.returncode == 0


def capture_pane(target: str) -> str:
    """Recent scrollback of a tmux pane as bounded text; "" on failure.

    tmux documents negative `-S` values as history lines and `-E -` as the end
    of the visible pane. The 2,000-line range covers tmux's default history
    limit without reading the entirety of a larger user-configured history.
    Retaining the first 1,048,576 characters favors the RC environment id
    printed near server startup.
    """
    cp = _tmux_run(
        [
            "capture-pane",
            "-p",
            "-S",
            f"-{_CAPTURE_HISTORY_LINES}",
            "-E",
            "-",
            "-t",
            target,
        ]
    )
    if cp is None or cp.returncode != 0:
        return ""
    return cp.stdout[:_CAPTURE_TEXT_CHAR_LIMIT]


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


def _tmux_list_all_panes() -> list[tuple[str, int]]:
    """[("session:window_index", pane_pid)] across ALL tmux sessions; [] on failure."""
    cp = _tmux_run(
        ["list-panes", "-a", "-F", "#{session_name}:#{window_index}\t#{pane_pid}"]
    )
    if cp is None:
        return []
    out: list[tuple[str, int]] = []
    for line in cp.stdout.splitlines():
        target, _, pid_s = line.partition("\t")
        try:
            out.append((target.strip(), int(pid_s.strip())))
        except ValueError:
            continue
    return out


def window_containing(panes: list[tuple[str, int]], ancestors: set[int]) -> str | None:
    """PURE: the pane target whose pane_pid appears in `ancestors`, or None.

    A session process hosted by a tmux pane has that pane's pid in its
    ancestor chain (the pane pid is the window's root process)."""
    for target, pane_pid in panes:
        if pane_pid in ancestors:
            return target
    return None


def residency_targets(pids: Iterable[int]) -> dict[int, str]:
    """{pid: "session:window_index"} for every pid hosted by a tmux pane.

    THE batch tmux-residency computation (ADR-0001 badge + actions share it):
    ONE `list-panes -a` for the whole pid set, then each pid's `/proc` ancestor
    chain is matched against the pane root pids — finds windows in ANY tmux
    session (per-project, rc, user-made). Misses are simply absent from the
    dict; tmux failure returns {} (swallow-errors contract)."""
    pid_list = list(pids)
    if not pid_list:
        return {}
    panes = _tmux_list_all_panes()
    if not panes:
        return {}
    out: dict[int, str] = {}
    for pid in pid_list:
        target = window_containing(panes, proc.ancestors_of(pid))
        if target:
            out[pid] = target
    return out


def find_session_window(pids: list[int]) -> str | None:
    """The tmux window ("session:index") hosting any of `pids`, or None.

    Single-target convenience over `residency_targets` (first hit in `pids`
    order) — used by the action layer when only one session is in play."""
    targets = residency_targets(pids)
    for pid in pids:
        if pid in targets:
            return targets[pid]
    return None


def select_window(target: str) -> bool:
    """tmux select-window -t <target>; False on failure (non-fatal for attach)."""
    cp = _tmux_run(["select-window", "-t", target])
    return cp is not None and cp.returncode == 0


def switch_client(target: str) -> bool:
    """tmux switch-client -t <target> — the inside-tmux attach equivalent."""
    cp = _tmux_run(["switch-client", "-t", target])
    return cp is not None and cp.returncode == 0
