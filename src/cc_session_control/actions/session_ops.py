"""Session operations: resume, terminate, delete, clipboard."""

from __future__ import annotations

import os
import shlex
import signal
import time
from dataclasses import dataclass
from typing import Literal

from .. import clipboard
from ..data import proc, rc
from ..data.liveness import invalidate_cache
from ..models import Session

TakeOverResult = Literal["killed", "gone", "refused", "failed"]


def take_over(pid: int, proc_start: str = "") -> TakeOverResult:
    """THE kill primitive behind every takeover/stop: R10 gate → kill-time
    liveness recheck → SIGTERM → settle → invalidate the liveness cache.

    One implementation so the gate order and the kill semantics cannot fork
    across the resume/terminate/stop variants (they had already started to:
    only terminate/stop skipped the settle sleep for an already-gone pid).
    The recheck (`pid_alive` against `proc_start`; mere existence when the
    start is unknown) closes the pid-reuse window — a confirm modal can sit
    open for minutes, and a recycled pid must never be SIGTERMed.

    Results: "killed" (signalled + settled), "gone" (already dead / recycled —
    nothing to kill), "refused" (R10: current undeterminable), "failed"
    (signal error, e.g. permissions). Fail-fast callers (terminate/stop) treat
    "failed" as failure; best-effort callers (the resume family) continue.
    """
    if not proc.current_determinable():
        return "refused"
    if not proc.pid_alive(pid, proc_start):
        invalidate_cache()  # already gone — liveness may have changed
        return "gone"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        invalidate_cache()
        return "gone"
    except Exception:
        return "failed"
    time.sleep(1)
    invalidate_cache()
    return "killed"


def terminate_session(s: Session) -> bool:
    """Send SIGTERM via `take_over` (which owns the R10 gate + the
    liveness-cache invalidation — terminating is the one session op that
    changes `claude agents` liveness; delete/cleanup only touch already-dead
    sessions, so they don't invalidate)."""
    if not s.pid:
        return False
    return take_over(s.pid, s.proc_start) in ("killed", "gone")


def _resume_plan(s: Session, fork: bool = False) -> tuple[str, list[str], bool]:
    """Shared resume recipe: the cwd to enter, the claude argv, and whether
    to kill the old session first.

    Returns (cwd, args, should_kill). Unified kill semantics: a fork is a copy
    and leaves the original running, while a plain resume takes the session
    over — so we kill only when it is alive, not the current session, and we
    are NOT forking. `resume_cmd` and `do_resume` both obey this single
    decision; they must not re-derive it.
    """
    args = ["claude", "--resume", s.sid]
    if fork:
        args.append("--fork-session")
    should_kill = s.alive and not s.current and not fork
    return s.cwd, args, should_kill


def would_take_over(s: Session, fork: bool = False) -> bool:
    """Whether resuming/relaunching `s` would first kill a live process (takeover).

    The single source of the "needs confirmation" decision for the UI: it reads
    `_resume_plan`'s `should_kill` so views never re-derive `s.alive and not
    s.current` themselves (CLAUDE.md: should_kill is single-point — re-derivation
    was the old divergence). `do_resume`/`relaunch_in_tmux` and the confirm gate
    thus agree by construction.
    """
    return _resume_plan(s, fork)[2]


def resume_cmd(s: Session, fork: bool = False) -> str:
    cwd, args, should_kill = _resume_plan(s, fork)
    parts: list[str] = []
    if should_kill and s.pid:  # never emit a bare `kill None` (L7)
        parts.append(f"kill {s.pid} && sleep 1")
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    parts.append(shlex.join(args))
    return " && ".join(parts)


def do_resume(s: Session, fork: bool = False) -> None:
    """chdir + (kill if needed) + exec claude. Does not return on success.

    R10: when a takeover kill is required but "current" can't be determined (no
    `/proc`), refuse — print a message and return WITHOUT killing or exec'ing, so
    we never SIGTERM the launching session (every pid looks dead off `/proc`).
    """
    cwd, args, should_kill = _resume_plan(s, fork)
    if should_kill and s.pid:
        if take_over(s.pid, s.proc_start) == "refused":
            print(
                "Refused: '/proc' unavailable — cannot determine the current "
                "session, so the old process can't be safely killed (R10)."
            )
            return
        # "gone"/"failed" fall through: the kill is best-effort here, the
        # resume itself must still happen.
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    os.execvp("claude", args)


def _rc_name(s: Session) -> str:
    """Remote-control label (shown in claude.ai/code) for a relaunched session."""
    base = s.cwd.rstrip("/").rsplit("/", 1)[-1] if s.cwd else ""
    return f"{base or 'session'}-{s.sid[:8]}"


def tmux_resume_cmd(s: Session, fork: bool = False) -> str:
    """Shell command that resumes the session under remote control."""
    cwd, args, _ = _resume_plan(s, fork)
    args = args + ["--remote-control", _rc_name(s)]
    line = shlex.join(args)
    return f"cd {shlex.quote(cwd)} && {line}" if cwd else line


def _spawn_in_tmux(s: Session, cmd: str, fork: bool = False) -> str | None:
    """Kill-if-takeover, then spawn `cmd` in the session's per-project tmux
    session — the shared skeleton behind `relaunch_in_tmux` (R key, with
    --remote-control) and `do_tmux_resume` (t key, foreground): the two
    differed only in the command builder. Returns the exact tmux target,
    or None on R10 refusal ("refused" from `take_over`) / tmux failure;
    "gone"/"failed" fall through like `do_resume` (best-effort kill).
    """
    _, _, should_kill = _resume_plan(s, fork)
    if should_kill and s.pid and take_over(s.pid, s.proc_start) == "refused":
        return None
    return rc.run_in_tmux(rc.session_name_for(s.cwd), s.sid[:8], cmd)


def relaunch_in_tmux(s: Session, fork: bool = False) -> bool:
    """Relaunch a session as `claude --resume … --remote-control …` inside a
    tmux window, so it outlives the terminal and is remotely controllable.

    A live, non-current session is taken over (old pid killed first, like
    terminate); a fork leaves the original running. csctl is NOT replaced —
    it just spawns the tmux window.
    """
    return _spawn_in_tmux(s, tmux_resume_cmd(s, fork), fork=fork) is not None


def attach_target(s: Session) -> str | None:
    """The tmux window ("session:index") already hosting this live session.

    Non-None means the `t` key can attach directly — no kill, no respawn (the
    session survives in place). None for dead sessions or live ones running in
    a bare terminal (those need `do_tmux_resume`)."""
    if not s.alive or not s.pid:
        return None
    return rc.find_session_window([s.pid])


def tmux_foreground_cmd(s: Session) -> str:
    """Shell command for the `t`-key window: plain resume, NO --remote-control.

    Deliberately unlike `tmux_resume_cmd` (the `R` key): a foreground tmux
    resume only needs the disconnect safety net; every --remote-control process
    mints a new cloud environment entry, which piles up with frequent use."""
    cwd, args, _ = _resume_plan(s)
    line = shlex.join(args)
    return f"cd {shlex.quote(cwd)} && {line}" if cwd else line


def do_tmux_resume(s: Session) -> str | None:
    """Kill-if-takeover, spawn the resume window in the session's per-project
    tmux session, and return the exact tmux target to enter; None on failure.
    The caller (cli) then calls `enter_window` on the target."""
    return _spawn_in_tmux(s, tmux_foreground_cmd(s))


def do_tmux_new(directory: str) -> str | None:
    """Start a NEW claude session in `directory`, inside that project's own
    tmux session, and return the exact tmux target to enter; None on failure.

    The 项目-tab `t` key: same skeleton as `do_tmux_resume` but nothing exists
    yet — no kill, no confirm, no R10 gate (no process is terminated). Plain
    `claude` with NO --remote-control (same tradeoff as `tmux_foreground_cmd`:
    every RC process mints a new cloud environment entry). No trust gate
    either: the user lands inside the window, so claude's own trust dialog
    shows interactively."""
    cmd = f"cd {shlex.quote(directory)} && claude"
    return rc.run_in_tmux(rc.session_name_for(directory), "claude", cmd)


# --- exit intents (the payload crossing the exit-then-exec seam) ------------
#
# The TUI cannot run `claude` (or replace itself with tmux) inside the urwid
# loop, so a resume-family action exits the MainLoop carrying ONE of these
# intents and `cli._cmd_tui` calls `intent.run()` afterwards. Each intent owns
# its own finalizer + failure messages, so adding a variant means adding one
# class here + one view call — app.py and cli.py stay untouched.

class ExitIntent:
    """What a view asks csctl to do AFTER the MainLoop exits."""

    def run(self) -> None:
        """Finalize outside the loop; may exec-replace the csctl process."""
        raise NotImplementedError


@dataclass(frozen=True)
class ResumeIntent(ExitIntent):
    """Enter/f: bare-terminal resume (execvp; takeover kill inside do_resume)."""

    session: Session
    fork: bool = False

    def run(self) -> None:
        do_resume(self.session, fork=self.fork)


@dataclass(frozen=True)
class AttachIntent(ExitIntent):
    """`t` on a tmux-hosted session: enter its window in place (no kill)."""

    target: str

    def run(self) -> None:
        if not enter_window(self.target):
            print(f"Failed to enter tmux window {self.target} (is tmux running?)")


@dataclass(frozen=True)
class TmuxResumeIntent(ExitIntent):
    """`t` on a dead / bare-terminal session: resume inside tmux, then enter."""

    session: Session

    def run(self) -> None:
        target = do_tmux_resume(self.session)
        if target is None:
            print("Failed to resume the session inside tmux (R10 degraded, or tmux unavailable).")
        elif not enter_window(target):
            print(f"Session resumed in tmux window {target}, but attaching failed.")


@dataclass(frozen=True)
class TmuxNewIntent(ExitIntent):
    """项目-tab `t`: start a NEW claude in tmux, then enter (pure spawn)."""

    directory: str

    def run(self) -> None:
        target = do_tmux_new(self.directory)
        if target is None:
            print("Failed to start a new session inside tmux (is tmux available?).")
        elif not enter_window(target):
            print(f"Session started in tmux window {target}, but attaching failed.")


def enter_window(target: str) -> bool:
    """Bring `target` ("session:window") to the user's terminal foreground.

    Outside tmux: exec `tmux attach-session` — replaces the csctl process (does
    not return on success). Inside tmux ($TMUX set): `switch-client`, then
    return so the caller exits csctl normally — both paths end csctl, keeping
    "接回 = 离开 csctl" uniform. select-window failure is non-fatal (the user
    lands in the session and can pick the window by hand)."""
    rc.select_window(target)
    session = target.split(":", 1)[0]
    if os.environ.get("TMUX"):
        return rc.switch_client(target)
    try:
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    except OSError:
        return False
    return True  # unreachable after a successful exec; keeps type-checkers calm


def to_clipboard(text: str) -> bool:
    return clipboard.copy(text)
