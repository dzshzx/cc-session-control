"""Generic tmux adapter — THE single tmux seam.

`_tmux_run_result` owns ordinary invocations. Compatibility wrappers keep the
empty/False/None views while typed operations retain expected external
failures. Add new tmux operations here, never as raw `subprocess` calls
elsewhere.

Bottom of the `data/` DAG: this module may import only `proc` from this
package (plus stdlib) — it knows nothing about RC servers or sessions. `rc.py`
consumes it for spawning/discovering managed RC-server windows;
`actions/session_ops.py` and `actions/agent_ops.py` consume it directly for
resume/relaunch tmux windows, deliberately without depending on `rc` (CONTEXT.md:
don't conflate tmux windows with Remote Control).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import proc
from .tmux_outcomes import (
    KillResult,
    KillState,
    PaneInventory,
    ResidencyInventory,
    ResidencyIssue,
    SessionWindowResult,
    TmuxIssue,
    TmuxPane,
    TmuxWindow,
    TmuxWriteResult,
    TmuxWriteStage,
    TmuxWriteState,
    WindowInventory,
    create_target_result,
    window_option_result,
)


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


_WINDOWS_FMT = (
    "#{window_id}\t#{window_name}\t#{pane_dead}\t#{pane_pid}"
    "\t#{@csctl_path}\t#{pane_current_path}"
)


def list_windows_inventory(session: str) -> WindowInventory:
    """Typed window inventory retaining external and malformed evidence.

    ONE tmux round-trip feeds both project↔window joins (`rc.scan_result`) and
    managed-server classification (`rc.scan_servers_result`)."""
    invocation = _tmux_run_result(["list-windows", "-t", session, "-F", _WINDOWS_FMT])
    cp = invocation.completed
    if cp is None:
        return WindowInventory(
            issues=(TmuxIssue("tmux list-windows", None, invocation.detail),),
        )
    if cp.returncode != 0:
        if _target_not_found(invocation.detail):
            return WindowInventory()
        return WindowInventory(
            issues=(TmuxIssue("tmux list-windows", None, invocation.detail),),
        )
    out: list[TmuxWindow] = []
    issues: list[TmuxIssue] = []
    for line_number, line in enumerate(cp.stdout.splitlines(), start=1):
        parts = line.split("\t", 5)
        if len(parts) != 6 or not parts[0] or parts[2] not in {"0", "1"}:
            issues.append(
                TmuxIssue(
                    "tmux list-windows",
                    None,
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
                    None,
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


def set_window_option_result(
    target: str,
    option: str,
    value: str,
) -> TmuxWriteResult:
    """Set one per-window option while retaining exact failure evidence."""
    invocation = _tmux_run_result(["set-option", "-w", "-t", target, option, value])
    cp = invocation.completed
    return window_option_result(
        target,
        None if cp is None else cp.returncode,
        invocation.detail,
    )


# -P -F makes tmux print the exact target of the window it just created, so
# callers enter THAT window even when names collide (no select-by-name guess).
_TARGET_FMT = "#{session_name}:#{window_index}"


def _spawn_result(
    invocation: _TmuxInvocation,
    stage: TmuxWriteStage,
) -> TmuxWriteResult:
    cp = invocation.completed
    return create_target_result(
        stage,
        None if cp is None else cp.returncode,
        "" if cp is None else cp.stdout,
        invocation.detail,
    )


def _tmux_new_window_result(session: str, name: str, cmd: str) -> TmuxWriteResult:
    invocation = _tmux_run_result(
        ["new-window", "-P", "-F", _TARGET_FMT, "-t", session, "-n", name, cmd]
    )
    return _spawn_result(invocation, TmuxWriteStage.NEW_WINDOW)


def _tmux_new_session_result(session: str, name: str, cmd: str) -> TmuxWriteResult:
    args = ["new-session", "-d", "-P", "-F", _TARGET_FMT]
    args.extend(["-s", session, "-n", name, cmd])
    invocation = _tmux_run_result(args)
    return _spawn_result(invocation, TmuxWriteStage.NEW_SESSION)


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


def kill_session_result(session: str) -> KillResult:
    """Kill one session while distinguishing absence from external failure."""
    return _kill_result(["kill-session", "-t", session], session)


def session_name_for(cwd: str) -> str:
    """tmux session name for a project directory: its basename, with the tmux
    target separators `.`/`:` (illegal in session names) replaced by `-`.

    One session per project — the grouping rule shared by every claude spawn
    (t 新建 / t 接回 / R 转入后台 / agents respawn). Empty cwd → "claude"."""
    base = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    name = base.replace(".", "-").replace(":", "-").strip()
    return name or "claude"


def run_in_tmux_result(session: str, window: str, cmd: str) -> TmuxWriteResult:
    """Create a target while preserving probe/create stage and diagnostics."""

    invocation = _tmux_run_result(["has-session", "-t", session])
    cp = invocation.completed
    if cp is None:
        return TmuxWriteResult(
            TmuxWriteStage.SESSION_PROBE,
            TmuxWriteState.FAILED,
            detail=invocation.detail,
        )
    if cp.returncode == 0:
        return _tmux_new_window_result(session, window, cmd)
    if not _target_not_found(invocation.detail):
        return TmuxWriteResult(
            TmuxWriteStage.SESSION_PROBE,
            TmuxWriteState.FAILED,
            detail=invocation.detail,
        )
    return _tmux_new_session_result(session, window, cmd)


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


def find_session_window_result(pids: list[int]) -> SessionWindowResult:
    """Typed first-target convenience over :func:`residency_inventory`."""

    inventory = residency_inventory(pids)
    for pid in pids:
        if pid in inventory.targets:
            return SessionWindowResult(inventory.targets[pid], inventory.issues)
    return SessionWindowResult(issues=inventory.issues)


def select_window(target: str) -> bool:
    """tmux select-window -t <target>; False on failure (non-fatal for attach)."""
    cp = _tmux_run(["select-window", "-t", target])
    return cp is not None and cp.returncode == 0


def switch_client(target: str) -> bool:
    """tmux switch-client -t <target> — the inside-tmux attach equivalent."""
    cp = _tmux_run(["switch-client", "-t", target])
    return cp is not None and cp.returncode == 0
