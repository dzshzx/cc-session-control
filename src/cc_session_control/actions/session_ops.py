"""Session operations: resume, terminate, delete, clipboard."""

from __future__ import annotations

import os
import shlex
import signal
import time

from .. import clipboard
from ..config import cfg
from ..data import proc, rc
from ..data.liveness import invalidate_cache
from ..models import Session


def terminate_session(s: Session) -> bool:
    """Send SIGTERM and own the liveness-cache invalidation.

    Terminating is the one session op that changes `claude agents` liveness,
    so it invalidates the alive_map cache itself — callers no longer have to
    remember to. (delete/cleanup only touch already-dead sessions, so they
    don't.)

    R10: refuses (returns False) when "current" can't be determined (no
    `/proc`) — we can't prove `s` is not the launching session, so a SIGTERM
    here could hit csctl's own session.
    """
    if not proc.current_determinable():
        return False
    if not s.pid:
        return False
    try:
        os.kill(s.pid, signal.SIGTERM)
    except ProcessLookupError:
        invalidate_cache()  # already gone — liveness changed
        return True
    except Exception:
        return False
    time.sleep(1)
    invalidate_cache()
    return True


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
    if should_kill:
        if not proc.current_determinable():
            print(
                "Refused: '/proc' unavailable — cannot determine the current "
                "session, so the old process can't be safely killed (R10)."
            )
            return
        try:
            os.kill(s.pid, signal.SIGTERM)
        except Exception:
            pass
        time.sleep(1)
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


def relaunch_in_tmux(s: Session, fork: bool = False) -> bool:
    """Relaunch a session as `claude --resume … --remote-control …` inside a
    tmux window, so it outlives the terminal and is remotely controllable.

    A live, non-current session is taken over (its old pid is killed first and
    the liveness cache invalidated, like terminate); a fork leaves the original
    running. csctl is NOT replaced — it just spawns the tmux window.

    R10: when a takeover kill is required but "current" can't be determined (no
    `/proc`), refuse (return False, do not kill or relaunch) — we can't prove `s`
    is not the launching session.
    """
    _, _, should_kill = _resume_plan(s, fork)
    if should_kill and s.pid:
        if not proc.current_determinable():
            return False
        try:
            os.kill(s.pid, signal.SIGTERM)
        except Exception:
            pass
        time.sleep(1)
        invalidate_cache()
    return rc.run_in_tmux(cfg.tmux_session, _rc_name(s), tmux_resume_cmd(s, fork))


def to_clipboard(text: str) -> bool:
    return clipboard.copy(text)
