"""Session scanning — scan Claude Code transcripts and determine status."""

from __future__ import annotations

import glob
import json
import os
import shutil
import time

from ..config import cfg
from ..models import Session
from .agents import alive_map

_NOISE = (
    "<command-message>", "<command-name>", "<command-args>",
    "<local-command-caveat>", "<local-command-stdout>", "<local-command-stderr>",
    "<system-reminder>", "caveat:",
)


def _is_noise(t: str) -> bool:
    t = t.strip().lower()
    return (not t) or any(t.startswith(n) for n in _NOISE)


def _clean_text(t: str) -> str:
    t = " ".join(t.split())
    for marker in ("<system-reminder", "<command-message", "<command-name",
                   "<command-args", "<local-command-"):
        i = t.find(marker)
        if i != -1:
            t = t[:i]
    return t.strip()


def _ancestor_pids() -> set[int]:
    pids = {os.getpid()}
    pid = os.getpid()
    for _ in range(40):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                data = fh.read()
            ppid = int(data[data.rfind(")") + 2:].split()[1])
        except Exception:
            break
        if ppid <= 1:
            break
        pids.add(ppid)
        pid = ppid
    return pids


def _parse_transcript(path: str, alive: dict[str, int], cur: set[int]) -> Session | None:
    """Parse one transcript .jsonl into a Session, or None if it has no cwd.

    `alive` is the {sid: pid} map and `cur` the ancestor-pid set; both are
    injected so this stays unit-testable. The substring-pre-check before
    json.loads is kept intact for performance.
    """
    sid = os.path.basename(path)[:-6]
    try:
        st = os.stat(path)
    except OSError:
        return None

    cwd = title = last_prompt = first_prompt = ""
    hidden: set[str] = set()
    prompts = 0

    try:
        with open(path, "r", errors="ignore") as fh:
            for line in fh:
                if '"sdk-ts"' in line:
                    hidden.add("sdk")
                if '"bridge-session"' in line:
                    hidden.add("bridge")
                if not cwd and '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd", "") or cwd
                    except Exception:
                        pass
                if '"aiTitle"' in line:
                    try:
                        title = json.loads(line).get("aiTitle", title) or title
                    except Exception:
                        pass
                if '"lastPrompt"' in line:
                    try:
                        last_prompt = json.loads(line).get("lastPrompt", last_prompt) or last_prompt
                    except Exception:
                        pass
                if '"type":"user"' in line:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") != "user":
                        continue
                    c = (o.get("message") or {}).get("content")
                    if isinstance(c, str):
                        texts = [c]
                    elif isinstance(c, list):
                        texts = [b.get("text", "") for b in c
                                 if isinstance(b, dict) and b.get("type") == "text"]
                    else:
                        texts = []
                    texts = [t for t in texts if t.strip()]
                    if texts:
                        prompts += 1
                        if not first_prompt:
                            for t in texts:
                                if _is_noise(t):
                                    continue
                                ct = _clean_text(t)
                                if ct:
                                    first_prompt = ct
                                    break
    except Exception:
        pass

    if not cwd:
        return None

    lp = "" if _is_noise(last_prompt) else last_prompt
    label = title or first_prompt or lp or "(untitled)"
    pid = alive.get(sid)

    return Session(
        sid=sid, cwd=cwd, label=label, mtime=st.st_mtime,
        prompts=prompts, pid=pid,
        alive=pid is not None,
        current=pid in cur if pid else False,
        hidden=hidden, file=path,
    )


def scan() -> list[Session]:
    root = str(cfg.projects_root)
    alive = alive_map()
    cur = _ancestor_pids()
    rows: list[Session] = []

    for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
        row = _parse_transcript(f, alive, cur)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: r.mtime, reverse=True)
    return rows


def cleanup_stats(sessions: list[Session]) -> dict[str, int]:
    claude_home = str(cfg.claude_home)
    total = len(sessions)
    empty = sum(1 for s in sessions if s.prompts == 0)
    short = sum(1 for s in sessions if 0 < s.prompts <= 2)
    orphan_dirs = 0
    all_sids = {s.sid for s in sessions}
    alive_sids = {s.sid for s in sessions if s.alive}
    for subdir in ("session-env", "file-history"):
        path = os.path.join(claude_home, subdir)
        if os.path.isdir(path):
            for name in os.listdir(path):
                if name not in all_sids and name not in alive_sids:
                    orphan_dirs += 1
    return {"total": total, "empty": empty, "short": short, "orphans": orphan_dirs}


def list_orphan_dirs(sessions: list[Session]) -> list[str]:
    claude_home = str(cfg.claude_home)
    all_sids = {s.sid for s in sessions}
    orphans: list[str] = []
    for subdir in ("session-env", "file-history"):
        path = os.path.join(claude_home, subdir)
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            if name not in all_sids:
                orphans.append(os.path.join(subdir, name))
    return sorted(set(orphans))


def remove_orphan_dirs(sessions: list[Session]) -> int:
    claude_home = str(cfg.claude_home)
    all_sids = {s.sid for s in sessions}
    count = 0
    for subdir in ("session-env", "file-history"):
        path = os.path.join(claude_home, subdir)
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            if name in all_sids:
                continue
            target = os.path.join(path, name)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
                count += 1
            elif os.path.isfile(target):
                try:
                    os.remove(target)
                    count += 1
                except OSError:
                    pass
    return count


def remove_session(s: Session) -> None:
    claude_home = str(cfg.claude_home)
    try:
        os.remove(s.file)
    except OSError:
        pass
    comp = s.file[:-6]
    if os.path.isdir(comp):
        shutil.rmtree(comp, ignore_errors=True)
    for p in (
        os.path.join(claude_home, "session-env", s.sid),
        os.path.join(claude_home, "file-history", s.sid),
        os.path.join(claude_home, "jobs", s.sid[:8]),
    ):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


def prune_sessions(sessions: list[Session], max_prompts: int = 0) -> list[Session]:
    """Return prunable sessions (not alive, not current, prompts <= max_prompts, not recently active)."""
    alive_sids = {s.sid for s in sessions if s.alive}
    now = time.time()
    return [
        s for s in sessions
        if s.prompts <= max_prompts
        and s.sid not in alive_sids
        and not s.current
        and (now - s.mtime) > 600
    ]
