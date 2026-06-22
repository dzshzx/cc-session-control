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


def resume_cmd(s: Session, fork: bool = False) -> str:
    parts: list[str] = []
    if s.alive and not s.current:
        parts.append(f"kill {s.pid} && sleep 1")
    if s.cwd:
        parts.append(f"cd {s.cwd}")
    r = f"claude --resume {s.sid}"
    if fork:
        r += " --fork-session"
    parts.append(r)
    return " && ".join(parts)


def do_resume(s: Session, fork: bool = False) -> None:
    """chdir + (kill if needed) + exec claude. Does not return."""
    if s.alive and not s.current and not fork:
        try:
            os.kill(s.pid, signal.SIGTERM)
        except Exception:
            pass
        time.sleep(1)
    if s.cwd and os.path.isdir(s.cwd):
        os.chdir(s.cwd)
    args = ["claude", "--resume", s.sid]
    if fork:
        args.append("--fork-session")
    os.execvp("claude", args)


def to_clipboard(text: str) -> bool:
    return clipboard.copy(text)
