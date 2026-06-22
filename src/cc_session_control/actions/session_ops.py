"""Session operations: resume, terminate, delete, clipboard."""

from __future__ import annotations

import os
import signal
import time

from .. import clipboard
from ..models import Session


def terminate_session(s: Session) -> bool:
    if not s.pid:
        return False
    try:
        os.kill(s.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    time.sleep(1)
    return True


def _resume_plan(s: Session, fork: bool = False) -> tuple[str, list[str]]:
    """Shared resume recipe: the cwd to enter and the claude argv.

    Returns (cwd, args). The kill-when-alive decision is intentionally NOT
    encoded here — see the divergence note on resume_cmd / do_resume below.
    """
    args = ["claude", "--resume", s.sid]
    if fork:
        args.append("--fork-session")
    return s.cwd, args


def resume_cmd(s: Session, fork: bool = False) -> str:
    cwd, args = _resume_plan(s, fork)
    parts: list[str] = []
    # DIVERGENCE (intentional-for-now): resume_cmd emits the kill prefix
    # whenever the session is alive & not current, REGARDLESS of fork —
    # while do_resume below skips the kill when fork=True. This latent
    # inconsistency is preserved as-is to honor no-behavior-change; a
    # follow-up task may unify it as a real behavior fix.
    if s.alive and not s.current:
        parts.append(f"kill {s.pid} && sleep 1")
    if cwd:
        parts.append(f"cd {cwd}")
    parts.append(" ".join(args))
    return " && ".join(parts)


def do_resume(s: Session, fork: bool = False) -> None:
    """chdir + (kill if needed) + exec claude. Does not return."""
    cwd, args = _resume_plan(s, fork)
    # DIVERGENCE (intentional-for-now): do_resume only kills when NOT forking,
    # whereas resume_cmd kills regardless of fork. See note on resume_cmd.
    if s.alive and not s.current and not fork:
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
