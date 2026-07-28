"""Session scanning — scan Claude Code transcripts and determine status."""

from __future__ import annotations

import glob
import json
import os
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import replace

from ..config import cfg
from ..models import LiveInfo, Session, SessionProc
from . import registry, tmux
from .liveness import (
    LivenessSnapshot,
    alive_map,
    is_rc_exposed,
    live_index,
    live_session_procs,
)
from .proc import ancestor_pids as _ancestor_pids  # /proc walk moved to proc.py

_NOISE = (
    "<command-message>",
    "<command-name>",
    "<command-args>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "caveat:",
)


def _is_noise(t: str) -> bool:
    t = t.strip().lower()
    return (not t) or any(t.startswith(n) for n in _NOISE)


def _clean_text(t: str) -> str:
    t = " ".join(t.split())
    for marker in (
        "<system-reminder",
        "<command-message",
        "<command-name",
        "<command-args",
        "<local-command-",
    ):
        i = t.find(marker)
        if i != -1:
            t = t[:i]
    return t.strip()


def _json_object(line: str) -> dict[str, object] | None:
    try:
        document = json.loads(line)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _parse_transcript(
    path: str,
    idx: dict[str, LiveInfo],
    cur: AbstractSet[int],
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
        with open(path, errors="ignore") as fh:
            for line in fh:
                if '"sdk-ts"' in line:
                    hidden.add("sdk")
                if '"bridge-session"' in line:
                    hidden.add("bridge")
                if not cwd and '"cwd"' in line:
                    document = _json_object(line)
                    candidate = document.get("cwd") if document is not None else None
                    if isinstance(candidate, str) and candidate:
                        cwd = candidate
                if '"aiTitle"' in line:
                    document = _json_object(line)
                    candidate = (
                        document.get("aiTitle") if document is not None else None
                    )
                    if isinstance(candidate, str) and candidate:
                        title = candidate
                if '"lastPrompt"' in line:
                    document = _json_object(line)
                    candidate = (
                        document.get("lastPrompt") if document is not None else None
                    )
                    if isinstance(candidate, str) and candidate:
                        last_prompt = candidate
                if '"type":"user"' in line:
                    document = _json_object(line)
                    if document is None:
                        continue
                    if document.get("type") != "user":
                        continue
                    message = document.get("message")
                    c = message.get("content") if isinstance(message, dict) else None
                    if isinstance(c, str):
                        texts = [c]
                    elif isinstance(c, list):
                        texts = []
                        for block in c:
                            if not isinstance(block, dict):
                                continue
                            text = block.get("text")
                            if block.get("type") == "text" and isinstance(text, str):
                                texts.append(text)
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
    except (OSError, UnicodeError):
        return None

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
        proc_start = info.proc_start
    else:
        pid = None
        alive = False
        kind = entrypoint = source = status = ""
        bridge = None
        current = False
        proc_alive = False
        proc_start = ""

    rc_exposed = is_rc_exposed(bridge, proc_alive)

    return Session(
        sid=sid,
        cwd=cwd,
        label=label,
        mtime=st.st_mtime,
        prompts=prompts,
        pid=pid,
        alive=alive,
        current=current,
        proc_start=proc_start,
        hidden=frozenset(hidden),
        file=path,
        kind=kind,
        entrypoint=entrypoint,
        source=source,
        rc_exposed=rc_exposed,
        env_id=bridge if rc_exposed else None,
        agent_short=sid[:8] if sid[:8] in job_shorts else None,
        status=status,
    )


def _candidate_pids(info: LiveInfo | None) -> tuple[int, ...]:
    """A LiveInfo's pid candidate set — `pids` when filled, else the chosen pid
    (same fallback rule the `current` check in `_parse_transcript` uses)."""
    if info is None:
        return ()
    if info.pids:
        return info.pids
    return (info.pid,) if info.pid else ()


def _inject_tmux_residency(
    rows: list[Session],
    idx: dict[str, LiveInfo],
) -> list[Session]:
    """Fill `Session.tmux_target` for every ALIVE session, in ONE batch.

    Collects all alive sessions' candidate pids, calls
    `tmux.residency_targets` once (one `list-panes -a` per scan cycle), and
    returns a replaced session carrying its first hit — any alive pid inside a
    tmux pane makes the session resident (ADR-0001). Dead sessions stay None;
    the badge and the resume/backgrounding actions read this SAME field, so
    there is no per-action re-detection (no second source of truth)."""
    alive_pids = {
        pid for row in rows if row.alive for pid in _candidate_pids(idx.get(row.sid))
    }
    targets = tmux.residency_targets(alive_pids)
    if not targets:
        return rows
    resident: list[Session] = []
    for row in rows:
        target = next(
            (
                targets[pid]
                for pid in _candidate_pids(idx.get(row.sid))
                if row.alive and pid in targets
            ),
            None,
        )
        resident.append(replace(row, tmux_target=target) if target else row)
    return resident


def scan(inputs: LivenessSnapshot | None = None) -> list[Session]:
    """Unified transcript-driven session scan.

    Merges the three liveness/identity sources once per scan — registry
    `sessions/<pid>.json`, `claude agents --json`, and `jobs/*/state.json` — then
    projects each transcript through `live_index()` to fill source/liveness/
    rc-exposure, and batch-injects tmux residency (`tmux_target`). Scan stays
    transcript-driven: an agent-only sid (present in the live index but with no
    transcript) is surfaced by the Agents tab, not here.

    When `inputs` is supplied by `build_world_snapshot`, this is an injected
    fast path: no registry or targeted `/proc` liveness read is repeated. With
    no injection, the standalone CLI-compatible path self-fetches as before.
    """
    root = str(cfg.projects_root)
    session_procs: Sequence[SessionProc]
    agents: Mapping[str, int | None]
    cur: AbstractSet[int]
    if inputs is None:
        session_procs = live_session_procs()
        agents = alive_map()
        job_shorts = {j.short for j in registry.read_agent_jobs()}
        cur = _ancestor_pids()
    else:
        session_procs = inputs.session_procs
        agents = inputs.agents_map
        job_shorts = {j.short for j in inputs.agent_jobs}
        cur = inputs.cur
    idx = live_index(session_procs, agents)
    rows: list[Session] = []

    for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
        row = _parse_transcript(f, idx, cur, job_shorts)
        if row is not None:
            rows.append(row)

    rows = _inject_tmux_residency(rows, idx)
    rows.sort(key=lambda r: r.mtime, reverse=True)
    return rows
