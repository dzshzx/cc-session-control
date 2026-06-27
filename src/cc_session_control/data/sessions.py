"""Session scanning — scan Claude Code transcripts and determine status."""

from __future__ import annotations

import glob
import json
import os
from dataclasses import replace

from ..config import cfg
from ..models import LiveInfo, Session
from . import registry
from .liveness import _is_rc_exposed, alive_map, live_index
from .proc import ancestor_pids as _ancestor_pids  # /proc walk moved to proc.py
from .proc import pid_alive

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


def _parse_transcript(
    path: str,
    idx: dict[str, LiveInfo],
    cur: set[int],
    job_shorts: set[str],
) -> Session | None:
    """Parse one transcript .jsonl into a Session, or None if it has no cwd.

    `idx` is the joined live index (sid -> LiveInfo from `live_index()`), `cur`
    the ancestor-pid set, and `job_shorts` the set of background-agent short ids
    (`sid[:8]`); all injected so this stays unit-testable. The substring
    pre-check before json.loads is kept intact for performance.
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

    # Join the merged liveness/identity for this sid. Missing => dead, no
    # registry data (transcript-only): liveness stays False and the registry-
    # derived fields stay empty.
    info = idx.get(sid)
    if info is not None:
        pid = info.pid
        alive = info.alive
        kind = info.kind
        entrypoint = info.entrypoint
        source = info.source
        status = info.status
        bridge = info.bridge
        # "current" must protect ANY of the sid's alive pids: a resumed session
        # has several pids and csctl may have been launched by an older one that
        # is NOT the newest `pid` chosen for display. Fall back to the single
        # chosen pid for hand-constructed LiveInfo with no `pids` list.
        cand = info.pids if info.pids else ([pid] if pid else [])
        current = any(p in cur for p in cand)
        proc_alive = info.proc_alive
    else:
        pid = None
        alive = False
        kind = entrypoint = source = status = ""
        bridge = None
        current = False
        proc_alive = False

    rc_exposed = _is_rc_exposed(bridge, proc_alive)

    return Session(
        sid=sid, cwd=cwd, label=label, mtime=st.st_mtime,
        prompts=prompts, pid=pid,
        alive=alive,
        current=current,
        hidden=hidden, file=path,
        kind=kind, entrypoint=entrypoint, source=source,
        rc_exposed=rc_exposed,
        env_id=bridge if rc_exposed else None,
        agent_short=sid[:8] if sid[:8] in job_shorts else None,
        status=status,
    )


def scan() -> list[Session]:
    """Unified transcript-driven session scan.

    Merges the three liveness/identity sources once per scan — registry
    `sessions/<pid>.json`, `claude agents --json`, and `jobs/*/state.json` — then
    projects each transcript through `live_index()` to fill source/liveness/
    rc-exposure. Scan stays transcript-driven: an agent-only sid (present in the
    live index but with no transcript) is surfaced by the Agents tab, not here.
    """
    root = str(cfg.projects_root)
    session_procs = [
        replace(sp, proc_alive=pid_alive(sp.pid, sp.proc_start))
        for sp in registry.read_session_procs()
    ]
    agents = alive_map()
    idx = live_index(session_procs, agents)
    job_shorts = {j.short for j in registry.read_agent_jobs()}
    cur = _ancestor_pids()
    rows: list[Session] = []

    for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
        row = _parse_transcript(f, idx, cur, job_shorts)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: r.mtime, reverse=True)
    return rows
