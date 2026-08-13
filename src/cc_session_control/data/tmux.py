"""Generic tmux adapter — THE single tmux seam.

`_tmux_run_result` owns ordinary invocations; destructive/result-bearing
operations consume its typed view and retain expected external failures.
`select_window`/`switch_client` are the one retained bool-view exception,
scoped to `enter_window`'s exec/attach navigation in
`actions/session_ops.py`: `select_window`'s failure is non-fatal and
discarded; `switch_client`'s bool is a single success/failure bit with no
typed detail consumer, whose failure the caller surfaces directly as an
"...attaching failed." message. Add new tmux operations here, never as raw
`subprocess` calls elsewhere.

Bottom of the `data/` DAG: this module may import only `proc` from this
package (plus stdlib) — it knows nothing about sessions.
`actions/session_ops.py` consumes it directly for resume/relaunch tmux
windows.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from . import proc
from .tmux_outcomes import (
    PaneInventory,
    ResidencyInventory,
    ResidencyIssue,
    SessionWindowResult,
    TmuxPane,
    TmuxWriteResult,
    TmuxWriteStage,
    TmuxWriteState,
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
    """Result-only view backing `select_window`/`switch_client` — the sole
    retained bool-view exception, scoped to the exec/attach navigation
    boundary (see module docstring)."""
    return _tmux_run_result(args).completed


def exact_session_target(session: str) -> str:
    """Encode a literal session name as an exact tmux target.

    tmux otherwise falls back from exact names to prefix and glob matching. The
    extra ``=`` is intentional when ``session`` itself starts with ``=``.
    """

    return f"={session}"


def _exact_window_target(target: str) -> str:
    """Encode a name/index target exactly; stable window IDs need no wrapper."""

    is_window_id = target.startswith("@") and target[1:].isdigit()
    return target if is_window_id else f"={target}"


def set_window_option_result(
    target: str,
    option: str,
    value: str,
) -> TmuxWriteResult:
    """Set one per-window option while retaining exact failure evidence."""
    invocation = _tmux_run_result(
        ["set-option", "-w", "-t", _exact_window_target(target), option, value]
    )
    cp = invocation.completed
    return window_option_result(
        target,
        None if cp is None else cp.returncode,
        invocation.detail,
    )


# -P -F makes tmux print the exact target of the window it just created, so
# callers enter THAT window even when names collide (no select-by-name guess).
# The trailing #{window_id} is the server-unique address follow-up window
# writes use: a racing kill/create can hand a name:index to a different
# window, a window id it cannot.
_TARGET_FMT = "#{session_name}:#{window_index}\t#{window_id}"


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


def _env_args(env: Mapping[str, str] | None) -> list[str]:
    """`-e KEY=VALUE` pairs for a spawn (ADR-0008).

    tmux takes these as ARGV, not shell text, so a value never passes
    through a shell. Setting them on the WINDOW (rather than prefixing the
    command) is deliberate: the identity then also applies to whatever the
    operator runs in that window afterwards — re-running codex in an eva
    window must not silently land on the default home.
    """
    if not env:
        return []
    return [
        arg for key, value in sorted(env.items()) for arg in ("-e", f"{key}={value}")
    ]


def _tmux_new_window_result(
    session: str,
    name: str,
    cmd: str,
    env: Mapping[str, str] | None = None,
) -> TmuxWriteResult:
    invocation = _tmux_run_result(
        [
            "new-window",
            "-P",
            "-F",
            _TARGET_FMT,
            "-t",
            exact_session_target(session),
            "-n",
            name,
            *_env_args(env),
            cmd,
        ]
    )
    return _spawn_result(invocation, TmuxWriteStage.NEW_WINDOW)


def _tmux_new_session_attempt(
    session: str,
    name: str,
    cmd: str,
    env: Mapping[str, str] | None = None,
) -> tuple[TmuxWriteResult, _TmuxInvocation]:
    args = ["new-session", "-d", "-P", "-F", _TARGET_FMT]
    args.extend(["-s", session, "-n", name, *_env_args(env), cmd])
    invocation = _tmux_run_result(args)
    return _spawn_result(invocation, TmuxWriteStage.NEW_SESSION), invocation


def _is_duplicate_session_failure(
    invocation: _TmuxInvocation,
    session: str,
) -> bool:
    cp = invocation.completed
    return (
        cp is not None
        and cp.returncode != 0
        and invocation.detail == f"duplicate session: {session}"
    )


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


def project_name_for(cwd: str) -> str:
    """Stable display name for a project directory.

    Uses the basename and replaces tmux target separators ``.``/``:`` with
    ``-``. The result prefixes session windows in the workbench session; it
    is display metadata, never the project's identity. Empty cwd → "claude".
    """
    base = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    name = base.replace(".", "-").replace(":", "-").strip()
    return name or "claude"


def window_name_for(cwd: str, leaf: str) -> str:
    """Project-visible window name inside the unified workbench session."""

    project = project_name_for(cwd)
    return f"{project}/{leaf}" if leaf else project


# C1: the dispatch-identity window options every csctl spawn declares and
# `list_panes_inventory` reads back — the tmux-metadata liveness source for
# CLIs whose processes rewrite their own argv (kimi title rewrite).
_SID_OPTION = "@csctl_sid"
_PROVIDER_OPTION = "@csctl_provider"


def _declare_dispatch_metadata(
    result: TmuxWriteResult,
    sid: str,
    provider: str,
) -> TmuxWriteResult:
    """Write the dispatch-identity options on a freshly created window.

    Writes address the server-unique `window_id`, never the name:index
    `target`: a racing kill/create can reassign that index, landing the
    options on a stranger window — a wrong kill target. A write failure
    never fails the spawn: the created target is returned with the failure
    retained in `detail` — the binding simply stays absent (fail safe). A
    partial write (one option only) also fails safe: the read side requires
    BOTH options to bind. A spawn printout without a window id skips the
    writes the same way — no binding beats a misaddressed one."""
    if not result.success or result.target is None:
        return result
    options = [
        (option, value)
        for option, value in ((_PROVIDER_OPTION, provider), (_SID_OPTION, sid))
        if value
    ]
    if not options:
        return result
    if result.window_id is None:
        return replace(
            result,
            detail="window metadata: spawn printed no window id; binding skipped",
        )
    failures: list[str] = []
    for option, value in options:
        write = set_window_option_result(result.window_id, option, value)
        if not write.success:
            failures.append(f"{option}: {write.detail or 'write failed'}")
    if not failures:
        return result
    return replace(result, detail="window metadata: " + "; ".join(failures))


def run_in_tmux_result(
    session: str,
    window: str,
    cmd: str,
    sid: str = "",
    provider: str = "",
    env: Mapping[str, str] | None = None,
) -> TmuxWriteResult:
    """Create a target while preserving probe/create stage and diagnostics.

    Non-empty `sid`/`provider` are declared on the created window as
    `@csctl_sid`/`@csctl_provider` (see `_declare_dispatch_metadata`); `env`
    is set on the created window itself (ADR-0008)."""

    invocation = _tmux_run_result(["has-session", "-t", exact_session_target(session)])
    cp = invocation.completed
    if cp is None:
        return TmuxWriteResult(
            TmuxWriteStage.SESSION_PROBE,
            TmuxWriteState.FAILED,
            detail=invocation.detail,
        )
    if cp.returncode == 0:
        created = _tmux_new_window_result(session, window, cmd, env)
    elif not _target_not_found(invocation.detail):
        return TmuxWriteResult(
            TmuxWriteStage.SESSION_PROBE,
            TmuxWriteState.FAILED,
            detail=invocation.detail,
        )
    else:
        created, new_session_invocation = _tmux_new_session_attempt(
            session, window, cmd, env
        )
        if _is_duplicate_session_failure(new_session_invocation, session):
            retry_probe = _tmux_run_result(
                ["has-session", "-t", exact_session_target(session)]
            )
            retry_cp = retry_probe.completed
            if retry_cp is not None and retry_cp.returncode == 0:
                created = _tmux_new_window_result(session, window, cmd, env)
    return _declare_dispatch_metadata(created, sid, provider)


_PANES_FMT = (
    "#{session_name}:#{window_index}\t#{pane_dead}\t#{pane_pid}"
    f"\t#{{{_SID_OPTION}}}\t#{{{_PROVIDER_OPTION}}}"
)


def list_panes_inventory() -> PaneInventory:
    """Typed global pane inventory for residency safety decisions.

    THE one pane walk (C1): each pane also carries the window's declared
    dispatch identity (`@csctl_sid`/`@csctl_provider`, "" when unset), so
    residency joins and metadata liveness binding share one tmux call.
    Dead panes (remain-on-exit residue) are excluded, not an issue: their
    recorded pid is vacated and may be reused by an unrelated process, so
    they must never feed a residency or metadata join."""

    invocation = _tmux_run_result(["list-panes", "-a", "-F", _PANES_FMT])
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
        parts = line.split("\t")
        target = parts[0].strip() if parts else ""
        dead = parts[1].strip() if len(parts) == 5 else ""
        try:
            pid = int(parts[2].strip()) if len(parts) == 5 else 0
        except ValueError:
            pid = 0
        if len(parts) != 5 or not target or pid <= 0 or dead not in {"0", "1"}:
            issues.append(
                ResidencyIssue(
                    "tmux list-panes",
                    None,
                    f"malformed row {line_number}: {line!r}",
                )
            )
            continue
        if dead == "1":
            continue
        out.append(TmuxPane(target, pid, parts[3].strip(), parts[4].strip()))
    return PaneInventory(tuple(out), tuple(issues))


def window_containing(
    panes: Sequence[TmuxPane],
    ancestors: set[int],
) -> str | None:
    """PURE: the pane target whose pane pid appears in `ancestors`, or None.

    A session process hosted by a tmux pane has that pane's pid in its
    ancestor chain (the pane pid is the window's root process)."""
    for pane in panes:
        if pane.pid in ancestors:
            return pane.target
    return None


def residency_inventory(pids: Iterable[int]) -> ResidencyInventory:
    """Typed pid→pane join retaining tmux and ancestor-scan uncertainty.

    THE batch tmux-residency computation (ADR-0001 badge + actions share it):
    ONE `list-panes -a` for the whole pid set, then each pid's `/proc` ancestor
    chain is matched against the pane root pids — finds windows in ANY tmux
    session (csctl workbench, legacy per-project, or user-made)."""
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
    cp = _tmux_run(["select-window", "-t", _exact_window_target(target)])
    return cp is not None and cp.returncode == 0


def switch_client(target: str) -> bool:
    """tmux switch-client -t <target> — the inside-tmux attach equivalent."""
    cp = _tmux_run(["switch-client", "-t", _exact_window_target(target)])
    return cp is not None and cp.returncode == 0
