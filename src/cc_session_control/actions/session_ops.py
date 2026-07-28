"""Session operations: resume, terminate, delete, clipboard."""

from __future__ import annotations

import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from .. import clipboard
from ..data import liveness, proc, tmux
from ..data.liveness import invalidate_cache
from ..models import Session

TakeOverResult = Literal["killed", "gone", "refused", "failed"]

#: The results a fail-fast caller counts as success (signalled, or nothing
#: left to signal) — single-sourced so terminate/stop can't diverge on it.
TAKE_OVER_OK = ("killed", "gone")


class TakeOverState(StrEnum):
    """Typed kill outcome used by every public destructive action."""

    KILLED = "killed"
    GONE = "gone"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class TakeOverOutcome:
    state: TakeOverState
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state in {TakeOverState.KILLED, TakeOverState.GONE}


def _proc_issue_detail(issues: tuple[proc.ProcIssue, ...]) -> str:
    parts = []
    for issue in issues:
        location = f" at {issue.path}" if issue.path else ""
        parts.append(f"{issue.source}{location}: {issue.detail}")
    return "; ".join(parts)


def take_over_result(pid: int, proc_start: str = "") -> TakeOverOutcome:
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
    ancestors = proc.probe_current_ancestors()
    if not ancestors.complete:
        return TakeOverOutcome(
            TakeOverState.REFUSED,
            _proc_issue_detail(ancestors.issues),
        )
    if pid in ancestors.pids:
        return TakeOverOutcome(
            TakeOverState.REFUSED,
            f"pid {pid} belongs to the current session ancestor chain",
        )
    probe = proc.probe_pid(pid, proc_start)
    if probe.alive is None:
        issue = probe.issue
        if issue is None:
            raise AssertionError("unknown pid probe must carry an issue")
        return TakeOverOutcome(
            TakeOverState.REFUSED,
            _proc_issue_detail((issue,)),
        )
    if not probe.alive:
        invalidate_cache()  # already gone — liveness may have changed
        return TakeOverOutcome(TakeOverState.GONE)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        invalidate_cache()
        return TakeOverOutcome(TakeOverState.GONE)
    except OSError as exc:
        return TakeOverOutcome(TakeOverState.FAILED, str(exc))
    time.sleep(1)
    invalidate_cache()
    return TakeOverOutcome(TakeOverState.KILLED)


def take_over(pid: int, proc_start: str = "") -> TakeOverResult:
    """Compatibility string view; safety decisions use :func:`take_over_result`."""
    return take_over_result(pid, proc_start).state.value


def terminate_session(s: Session) -> bool:
    """Send SIGTERM via `take_over_result` (which owns the R10 gate + the
    liveness-cache invalidation — terminating is the one session op that
    changes `claude agents` liveness; delete/cleanup only touch already-dead
    sessions, so they don't invalidate)."""
    if not s.pid:
        return False
    return take_over_result(s.pid, s.proc_start).success


def _resume_plan(s: Session, fork: bool = False) -> tuple[str, list[str], bool]:
    """Shared resume recipe: the cwd to enter, the claude argv, and whether
    to kill the old session first.

    Returns (cwd, args, should_kill). Unified kill semantics: a fork is a copy
    and leaves the original running, while a plain resume takes the session
    over — so we kill only when it is alive, not the current session, and we
    are NOT forking. `resume_cmd` and `do_resume_result` both obey this single
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
    was the old divergence). The typed resume operations and the confirm gate
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


@dataclass(frozen=True)
class ResumeOutcome:
    success: bool
    detail: str = ""


@dataclass(frozen=True)
class TmuxResumeOutcome:
    target: str | None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.target is not None


def _liveness_issue_detail(evidence: liveness.LivenessSnapshot) -> str:
    parts = []
    for issue in evidence.issues:
        location = f" at {issue.path}" if issue.path else ""
        parts.append(f"{issue.source}{location}: {issue.detail}")
    return "; ".join(parts)


def _resume_liveness_gate() -> str:
    evidence = liveness.liveness_inputs()
    return "" if evidence.complete else _liveness_issue_detail(evidence)


def do_resume_result(s: Session, fork: bool = False) -> ResumeOutcome:
    """chdir + (kill if needed) + exec claude, with a typed refusal outcome.

    R10: when a takeover kill is required but "current" can't be determined (no
    `/proc`), refuse WITHOUT killing or exec'ing, so we never SIGTERM the
    launching session (every pid looks dead off `/proc`). A successful exec
    replaces csctl and never returns; True is the modeled-success return for
    tests whose system boundary returns.
    """
    incomplete = _resume_liveness_gate()
    if incomplete:
        return ResumeOutcome(False, incomplete)
    cwd, args, should_kill = _resume_plan(s, fork)
    if should_kill and s.pid:
        takeover = take_over_result(s.pid, s.proc_start)
        if takeover.state is TakeOverState.REFUSED:
            return ResumeOutcome(False, takeover.detail)
        # "gone"/"failed" fall through: the kill is best-effort here, the
        # resume itself must still happen.
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    os.execvp("claude", args)
    return ResumeOutcome(True)


def do_resume(s: Session, fork: bool = False) -> bool:
    """Compatibility bool view; public intents use :func:`do_resume_result`."""
    return do_resume_result(s, fork).success


def _spawn_in_tmux_result(
    s: Session,
    cmd: str,
    fork: bool = False,
) -> TmuxResumeOutcome:
    """Kill-if-takeover, then spawn `cmd` in the session's per-project tmux
    session — the shared skeleton behind `do_tmux_resume` (Enter/f/R, the
    tmux-first dispatch verbs, ADR-0001). A fork is a copy (never kills) and
    gets its own `<sid8>-fork` window so it doesn't shadow the original's.
    Returns a typed outcome containing the exact tmux target, or a detail on
    liveness refusal or tmux failure. "gone"/"failed" takeover results fall
    through like :func:`do_resume_result` (best-effort kill).
    """
    incomplete = _resume_liveness_gate()
    if incomplete:
        return TmuxResumeOutcome(None, incomplete)
    _, _, should_kill = _resume_plan(s, fork)
    if should_kill and s.pid:
        takeover = take_over_result(s.pid, s.proc_start)
        if takeover.state is TakeOverState.REFUSED:
            return TmuxResumeOutcome(None, takeover.detail)
    window = f"{s.sid[:8]}-fork" if fork else s.sid[:8]
    target = tmux.run_in_tmux(tmux.session_name_for(s.cwd), window, cmd)
    detail = "" if target is not None else "tmux unavailable"
    return TmuxResumeOutcome(target, detail)


def attach_target(s: Session) -> str | None:
    """The tmux window ("session:index") already hosting this live session.

    Non-None means the session is tmux-resident and can be entered in place —
    no kill, no respawn. Reads the snapshot-computed `Session.tmux_target`
    (the SAME data the ⧉ badge renders — one source, no per-action
    re-detection), guarded on `alive` so a stale target on a dead session
    never answers."""
    if not s.alive:
        return None
    return s.tmux_target


def tmux_foreground_cmd(s: Session, fork: bool = False) -> str:
    """Shell command for a tmux-dispatch window: plain resume, NO
    --remote-control.

    Deliberate (ADR-0001): tmux residency is the anti-disconnect mechanism;
    every --remote-control process mints a new cloud environment entry, which
    piles up with frequent use — the Sessions tab never mints cloud envs."""
    cwd, args, _ = _resume_plan(s, fork)
    line = shlex.join(args)
    return f"cd {shlex.quote(cwd)} && {line}" if cwd else line


def do_tmux_resume(s: Session, fork: bool = False) -> str | None:
    """Kill-if-takeover (fork never kills), spawn the resume window in the
    session's per-project tmux session, and return the exact tmux target;
    None on failure. Enter/f enter the target afterwards (`TmuxResumeIntent`);
    R 转后台 spawns it and stays in csctl."""
    return do_tmux_resume_result(s, fork).target


def do_tmux_resume_result(s: Session, fork: bool = False) -> TmuxResumeOutcome:
    """Typed tmux resume outcome retaining probe or spawn failure detail."""
    return _spawn_in_tmux_result(s, tmux_foreground_cmd(s, fork), fork=fork)


def do_tmux_new(directory: str) -> str | None:
    """Start a NEW claude session in `directory`, inside that project's own
    tmux session, and return the exact tmux target to enter; None on failure.

    The 项目-tab Enter key: same skeleton as `do_tmux_resume` but nothing exists
    yet — no kill, no confirm, no R10 gate (no process is terminated). Plain
    `claude` with NO --remote-control (same tradeoff as `tmux_foreground_cmd`:
    every RC process mints a new cloud environment entry). No trust gate
    either: the user lands inside the window, so claude's own trust dialog
    shows interactively."""
    cmd = f"cd {shlex.quote(directory)} && claude"
    return tmux.run_in_tmux(tmux.session_name_for(directory), "claude", cmd)


# --- exit intents (the payload crossing the exit-then-exec seam) ------------
#
# The TUI cannot run `claude` (or replace itself with tmux) inside the urwid
# loop, so a resume-family action exits the MainLoop carrying ONE of these
# intents and the CLI TUI handler calls `intent.run()` afterwards. Each
# intent owns its own finalizer + failure messages, so adding a variant means
# adding one class here + one view call — app.py and cli.py stay untouched.


class ExitIntent:
    """What a view asks csctl to do AFTER the MainLoop exits."""

    def run(self) -> int:
        """Finalize outside the loop and return a process exit status."""
        raise NotImplementedError


@dataclass(frozen=True)
class ResumeIntent(ExitIntent):
    """`t` 终端接回: bare-terminal resume (execvp; takeover kill inside
    do_resume_result) — the fallback when tmux is unavailable or unwanted."""

    session: Session
    fork: bool = False

    def run(self) -> int:
        try:
            outcome = do_resume_result(self.session, fork=self.fork)
        except OSError as exc:
            print(
                f"Failed to resume session {self.session.sid} in the terminal: {exc}",
                file=sys.stderr,
            )
            return 1
        if not outcome.success:
            detail = f" {outcome.detail}" if outcome.detail else ""
            print(
                "Refused: liveness evidence incomplete; the session was not "
                f"resumed.{detail}",
                file=sys.stderr,
            )
            return 1
        return 0


@dataclass(frozen=True)
class AttachIntent(ExitIntent):
    """Enter on a tmux-resident session: enter its window in place (no kill)."""

    target: str

    def run(self) -> int:
        try:
            entered = enter_window(self.target)
        except OSError as exc:
            print(
                f"Failed to enter tmux window {self.target}: {exc}",
                file=sys.stderr,
            )
            return 1
        if not entered:
            print(
                f"Failed to enter tmux window {self.target} (is tmux running?)",
                file=sys.stderr,
            )
            return 1
        return 0


@dataclass(frozen=True)
class TmuxResumeIntent(ExitIntent):
    """Enter/f on a dead / bare-terminal session: resume (or fork) inside
    tmux, then enter — the primary tmux-first dispatch (ADR-0001)."""

    session: Session
    fork: bool = False

    def run(self) -> int:
        outcome = do_tmux_resume_result(self.session, fork=self.fork)
        if outcome.target is None:
            detail = f": {outcome.detail}" if outcome.detail else ""
            print(
                f"Failed to resume the session inside tmux{detail}.",
                file=sys.stderr,
            )
            return 1
        target = outcome.target
        try:
            entered = enter_window(target)
        except OSError as exc:
            print(
                f"Session resumed in tmux window {target}, but attaching failed: {exc}",
                file=sys.stderr,
            )
            return 1
        if not entered:
            print(
                f"Session resumed in tmux window {target}, but attaching failed.",
                file=sys.stderr,
            )
            return 1
        return 0


@dataclass(frozen=True)
class TmuxNewIntent(ExitIntent):
    """项目-tab Enter: start a NEW claude in tmux, then enter (pure spawn)."""

    directory: str

    def run(self) -> int:
        target = do_tmux_new(self.directory)
        if target is None:
            print(
                "Failed to start a new session inside tmux (is tmux available?).",
                file=sys.stderr,
            )
            return 1
        try:
            entered = enter_window(target)
        except OSError as exc:
            print(
                f"Session started in tmux window {target}, but attaching failed: {exc}",
                file=sys.stderr,
            )
            return 1
        if not entered:
            print(
                f"Session started in tmux window {target}, but attaching failed.",
                file=sys.stderr,
            )
            return 1
        return 0


def enter_window(target: str) -> bool:
    """Bring `target` ("session:window") to the user's terminal foreground.

    Outside tmux: exec `tmux attach-session` — replaces the csctl process (does
    not return on success). Inside tmux ($TMUX set): `switch-client`, then
    return so the caller exits csctl normally — both paths end csctl, keeping
    "接回 = 离开 csctl" uniform. select-window failure is non-fatal (the user
    lands in the session and can pick the window by hand)."""
    tmux.select_window(target)
    session = target.split(":", 1)[0]
    if os.environ.get("TMUX"):
        return tmux.switch_client(target)
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
    return True  # unreachable after a successful exec; keeps type-checkers calm


def to_clipboard(text: str) -> bool:
    return clipboard.copy(text)
