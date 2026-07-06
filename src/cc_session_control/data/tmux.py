"""Generic tmux adapter — THE single tmux seam.

Only `_tmux_run` touches `subprocess`; every other tmux call is a thin verb
wrapper that keeps the swallow-errors contract (return empty/False/None on any
failure). Add new tmux operations as wrappers here, not raw `subprocess` calls
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

from . import proc


def _tmux_run(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run one `tmux <args>` command; return the result, or None on failure."""
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None


def list_windows(session: str) -> list[str]:
    cp = _tmux_run(["list-windows", "-t", session, "-F", "#W"])
    if cp is None:
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def pane_alive(target: str) -> bool:
    cp = _tmux_run(["list-panes", "-t", target, "-F", "#{pane_dead}"])
    if cp is None:
        return False
    return cp.stdout.strip().split("\n")[0] == "0"


def window_pids(session: str) -> dict[str, int]:
    """{window_name: pane_pid} for `session`; {} on failure.

    The pane pid IS the hosted process's pid because tmux spawns like
    `start_one` run `exec claude …` (the shell is replaced). Used to classify
    /proc-discovered servers as managed (pid in this set) vs external.
    """
    cp = _tmux_run(["list-windows", "-t", session, "-F", "#W\t#{pane_pid}"])
    if cp is None:
        return {}
    out: dict[str, int] = {}
    for line in cp.stdout.splitlines():
        name, _, pid_s = line.partition("\t")
        name = name.strip()
        try:
            pid = int(pid_s.strip())
        except ValueError:
            continue
        if name:
            out[name] = pid
    return out


def capture_pane(target: str) -> str:
    """Full scrollback of a tmux pane as text; "" on failure.

    Captures from the start of history (`-S -`) so an `env_*` id printed at
    server startup is still grep-able after it scrolls off the visible region.
    """
    cp = _tmux_run(["capture-pane", "-p", "-S", "-", "-t", target])
    if cp is None:
        return ""
    return cp.stdout


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
    cp = _tmux_run(["new-window", "-P", "-F", _TARGET_FMT, "-t", session, "-n", name, cmd])
    return _spawned_target(cp)


def _tmux_new_session(session: str, name: str, cmd: str) -> str | None:
    """Create a detached session; return its window's target, or None."""
    cp = _tmux_run(["new-session", "-d", "-P", "-F", _TARGET_FMT, "-s", session, "-n", name, cmd])
    return _spawned_target(cp)


def kill_window(target: str) -> bool:
    cp = _tmux_run(["kill-window", "-t", target])
    return cp is not None and cp.returncode == 0


def kill_session(session: str) -> bool:
    cp = _tmux_run(["kill-session", "-t", session])
    return cp is not None and cp.returncode == 0


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


def window_containing(
    panes: list[tuple[str, int]], ancestors: set[int]
) -> str | None:
    """PURE: the pane target whose pane_pid appears in `ancestors`, or None.

    A session process hosted by a tmux pane has that pane's pid in its
    ancestor chain (the pane pid is the window's root process)."""
    for target, pane_pid in panes:
        if pane_pid in ancestors:
            return target
    return None


def find_session_window(pids: list[int]) -> str | None:
    """The tmux window ("session:index") hosting any of `pids`, or None.

    Walks each pid's `/proc` ancestor chain and matches it against every tmux
    pane's root pid — finds windows in ANY tmux session (cc, rc, user-made)."""
    panes = _tmux_list_all_panes()
    if not panes:
        return None
    for pid in pids:
        target = window_containing(panes, proc.ancestors_of(pid))
        if target:
            return target
    return None


def select_window(target: str) -> bool:
    """tmux select-window -t <target>; False on failure (non-fatal for attach)."""
    cp = _tmux_run(["select-window", "-t", target])
    return cp is not None and cp.returncode == 0


def switch_client(target: str) -> bool:
    """tmux switch-client -t <target> — the inside-tmux attach equivalent."""
    cp = _tmux_run(["switch-client", "-t", target])
    return cp is not None and cp.returncode == 0
