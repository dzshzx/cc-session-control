"""Session operations: resume, terminate, delete, clipboard."""

from __future__ import annotations

import os
import signal
import time

from .. import clipboard
from ..data.agents import invalidate_cache
from ..models import Session


def terminate_session(s: Session) -> bool:
    """Send SIGTERM and own the liveness-cache invalidation.

    Terminating is the one session op that changes `claude agents` liveness,
    so it invalidates the alive_map cache itself — callers no longer have to
    remember to. (delete/cleanup only touch already-dead sessions, so they
    don't.)
    """
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


def resume_cmd(s: Session, fork: bool = False) -> str:
    cwd, args, should_kill = _resume_plan(s, fork)
    parts: list[str] = []
    if should_kill:
        parts.append(f"kill {s.pid} && sleep 1")
    if cwd:
        parts.append(f"cd {cwd}")
    parts.append(" ".join(args))
    return " && ".join(parts)


def do_resume(s: Session, fork: bool = False) -> None:
    """chdir + (kill if needed) + exec claude. Does not return."""
    cwd, args, should_kill = _resume_plan(s, fork)
    if should_kill:
        try:
            os.kill(s.pid, signal.SIGTERM)
        except Exception:
            pass
        time.sleep(1)
    if cwd and os.path.isdir(cwd):
        os.chdir(cwd)
    os.execvp("claude", args)


def to_clipboard(text: str) -> bool:
    return clipboard.copy(text)
