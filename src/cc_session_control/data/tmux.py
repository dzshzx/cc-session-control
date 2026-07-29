"""Generic tmux adapter — THE single tmux seam.

`_tmux_run_result` owns ordinary invocations; `capture_pane_result` owns bounded
streaming capture. Compatibility wrappers keep the empty/False/None views while
typed operations retain expected external failures. Add new tmux operations
here, never as raw `subprocess` calls elsewhere.

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import proc
from .tmux_outcomes import (
    KillResult,
    KillState,
    PaneCaptureIssue,
    PaneCaptureResult,
    PaneInventory,
    ResidencyInventory,
    ResidencyIssue,
    SessionWindowResult,
    TmuxIssue,
    TmuxPane,
    TmuxWindow,
    TmuxWriteOperation,
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
_CAPTURE_HISTORY_LINES = 2_000
_CAPTURE_START = f"-{_CAPTURE_HISTORY_LINES}"
_CAPTURE_BYTE_LIMIT = 1_048_576
_CAPTURE_READ_SIZE = 64 * 1_024
_CAPTURE_DETAIL_LIMIT = 512
_TMUX_TIMEOUT_SECONDS = 5.0
_REAP_GRACE_SECONDS = 0.2


def _capture_failure_detail(label: str, exc: BaseException) -> str:
    raw = " ".join(str(exc).split()) or type(exc).__name__
    return f"{label}: {raw}"[:_CAPTURE_DETAIL_LIMIT]


def _close_selector(selector: selectors.BaseSelector) -> str:
    try:
        selector.close()
    except OSError as exc:
        return _capture_failure_detail("selector close failed", exc)
    return ""


def _pane_failure(target: str, detail: str) -> PaneCaptureResult:
    issue = PaneCaptureIssue(
        "tmux capture-pane", target, detail[:_CAPTURE_DETAIL_LIMIT]
    )
    return PaneCaptureResult(target, issue=issue)


def _pane_success(
    target: str,
    output: bytearray,
    *,
    truncated: bool = False,
) -> PaneCaptureResult:
    decoded = output.decode("utf-8", errors="ignore")
    text = "".join(decoded.splitlines(keepends=True)[:_CAPTURE_HISTORY_LINES])
    return PaneCaptureResult(target, text, truncated=truncated)


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
    """Compatibility bool view of :func:`set_window_option_result`."""
    return set_window_option_result(target, option, value).success


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


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> str:
    """Stop a bounded capture and consume its child state; detail wait failure."""
    detail = ""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=_REAP_GRACE_SECONDS)
        return ""
    except subprocess.TimeoutExpired:
        pass
    except (OSError, subprocess.SubprocessError) as exc:
        detail = _capture_failure_detail("wait/reap failed", exc)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait()
    except (OSError, subprocess.SubprocessError) as exc:
        return _capture_failure_detail("wait/reap failed", exc)
    return detail


def capture_pane_result(target: str) -> PaneCaptureResult:
    """Capture bounded pane text, retaining expected external failures."""
    try:
        selector = selectors.DefaultSelector()
    except OSError as exc:
        return _pane_failure(target, _capture_failure_detail("selector failed", exc))
    try:
        process = subprocess.Popen(
            [
                "tmux",
                "capture-pane",
                "-p",
                "-S",
                _CAPTURE_START,
                "-E",
                "-",
                "-t",
                target,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _close_selector(selector)
        return _pane_failure(target, _capture_failure_detail("spawn failed", exc))
    stdout, stderr = process.stdout, process.stderr
    if stdout is None or stderr is None:
        cleanup_detail = _terminate_and_reap(process)
        close_detail = _close_selector(selector)
        return _pane_failure(
            target,
            cleanup_detail or close_detail or "spawn returned no stdout/stderr pipe",
        )

    output = bytearray()
    deadline = time.monotonic() + _TMUX_TIMEOUT_SECONDS
    hit_limit = False
    detail = ""
    close_detail = ""
    try:
        try:
            selector.register(stdout, selectors.EVENT_READ, "stdout")
            selector.register(stderr, selectors.EVENT_READ, "stderr")
        except OSError as exc:
            detail = _capture_failure_detail("selector failed", exc)
        while selector.get_map() and not detail:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                detail = f"timed out after {_TMUX_TIMEOUT_SECONDS:g} seconds"
                break
            try:
                events = selector.select(timeout)
            except OSError as exc:
                detail = _capture_failure_detail("selector failed", exc)
                break
            if not events:
                detail = f"timed out after {_TMUX_TIMEOUT_SECONDS:g} seconds"
                break
            for key, _mask in events:
                read_size = (
                    min(_CAPTURE_READ_SIZE, _CAPTURE_BYTE_LIMIT - len(output))
                    if key.data == "stdout"
                    else _CAPTURE_READ_SIZE
                )
                try:
                    chunk = os.read(key.fd, read_size)
                except OSError as exc:
                    detail = _capture_failure_detail("read failed", exc)
                    break
                if chunk and key.data == "stdout":
                    output.extend(chunk)
                    hit_limit = len(output) == _CAPTURE_BYTE_LIMIT
                elif not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except OSError as exc:
                        detail = _capture_failure_detail("selector failed", exc)
                if hit_limit or detail:
                    break
            if hit_limit or detail:
                break
    finally:
        close_detail = _close_selector(selector)
    detail = detail or close_detail

    if detail or hit_limit:
        cleanup_detail = _terminate_and_reap(process)
        stdout.close()
        stderr.close()
        if hit_limit and not detail and not cleanup_detail:
            return _pane_success(target, output, truncated=True)
        return _pane_failure(target, detail or cleanup_detail)

    try:
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate_and_reap(process)
        return _pane_failure(
            target,
            f"timed out after {_TMUX_TIMEOUT_SECONDS:g} seconds",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = _capture_failure_detail("wait failed", exc)
        _terminate_and_reap(process)
        return _pane_failure(target, detail)
    finally:
        stdout.close()
        stderr.close()
    if returncode != 0:
        return _pane_failure(target, f"exited with status {returncode}")
    return _pane_success(target, output)


def capture_pane(target: str) -> str:
    """Compatibility text-only view of :func:`capture_pane_result`."""

    return capture_pane_result(target).text


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
    """Compatibility target-only view of :func:`run_in_tmux_result`."""
    result = run_in_tmux_result(session, window, cmd)
    return result.target if result.success else None


def run_in_tmux_result(session: str, window: str, cmd: str) -> TmuxWriteResult:
    """Create a target while preserving probe/create stage and diagnostics."""

    invocation = _tmux_run_result(["has-session", "-t", session])
    cp = invocation.completed
    if cp is None:
        return TmuxWriteResult(
            TmuxWriteOperation.CREATE_TARGET,
            TmuxWriteStage.SESSION_PROBE,
            TmuxWriteState.FAILED,
            detail=invocation.detail,
        )
    if cp.returncode == 0:
        return _tmux_new_window_result(session, window, cmd)
    if not _target_not_found(invocation.detail):
        return TmuxWriteResult(
            TmuxWriteOperation.CREATE_TARGET,
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
