"""kimi SessionStart/SessionEnd hook endpoint — the runtime-registry writer.

kimi's official hook seam (verified on 0.34.0 2026-08-12, re-verified on
0.35.0 2026-08-13): a `[[hooks]]` rule in `~/.kimi-code/config.toml` pipes
an event payload (JSON, stdin) to `csctl _kimi-hook`; this module turns it
into the runtime registry the kimi provider binds from
(`data/providers/kimi.py::_read_registry` — the same pid-keyed shape as
Claude's `sessions/<pid>.json`). The payload keys are snake_case: 0.35.0
builds them camelCase and runs `toHookInputData`/`camelToSnake` before
spawning the command, so `hook_event_name`/`session_id` stay the contract.

- SessionStart (payload: `session_id`, `cwd`, `source` startup|resume)
  writes `run/<pid>.json` = {"sessionId", "procStart"}. It fires when the
  session materializes, which is NOT always the first prompt: `--prompt`
  and a `--session` RESUME both register immediately (0.35.0, 2026-08-13 —
  a resumed TUI had its entry before any input). Only a NEW session waits
  for its first prompt, because that is when its sid comes into existence.
  So the unbindable window is narrower than "every TUI until it is used":
  it is new sessions, pre-first-prompt.
- SessionHeartbeat (payload adds `uptime_ms`) re-registers the hosting
  session through the SAME write path: kimi fires it every 60 s while the
  session is alive, and the timer only runs when the event is configured
  (0.36.1 docs). SessionStart delivery is fire-and-forget and empirically
  NOT guaranteed — a live 0.36.1 session went unbound on 2026-08-16 with
  zero trace (no entry, no error-log line, hook verified working before
  and after) — so the heartbeat is what turns a missed start into a
  ≤60 s unbound window instead of an unbound lifetime.
- SessionEnd removes it — but do NOT count on it: 0.35.0 left the entry in
  place after a clean `kimi -p` exit and after a killed TUI (2026-08-13),
  so entries accumulate. `kimi.prune_gone_entries` is what actually bounds
  the directory (it owns the `<pid>.json` name contract, so this module
  never re-derives it); the reader's starttime recheck keeps leftovers
  inert.
- Any other event is ignored (forward-compatible with future rules) but
  recorded — an unknown event is how a renamed payload key would first
  show up, and silence is exactly what made the 2026-08-13 miss unreadable.

The hosting kimi pid is the hook's grandparent (kimi → `sh -c` → this
process — the one-hop shell layer verified on 0.34.0, re-verified 0.35.0).
The reader side re-verifies identity (argv0/comm/exe) and starttime per
entry, so a wrong or forged write can never mint a binding — fail closed by
construction.

Exit codes: 0 processed / deliberately ignored; 2 malformed payload;
3 hosting-process ancestry unreadable; 4 registry write/remove failed.
Nobody reads them — kimi swallows hook failures (`trigger(...).catch(() =>
[])`) — so every non-registering outcome also appends one bounded line to
`run/hook-errors.log`. That trail is the only evidence a registration was
ever attempted.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from ..config import cfg
from ..data import proc
from ..data.atomic_write import AtomicWriteError, advisory_lock, atomic_replace
from ..data.proc import ProcReadState
from ..data.providers.kimi import REGISTRY_DIR, prune_gone_entries

#: Diagnostic trail for hook runs that did not register a session, kept next
#: to the registry it explains. Bounded: the newest lines win.
ERROR_LOG = "hook-errors.log"
MAX_ERROR_LINES = 50
#: Cap on the echoed event name. `hook_event_name` is arbitrary payload JSON:
#: unbounded or newline-bearing text would evict real evidence from a trail
#: that only keeps `MAX_ERROR_LINES`.
MAX_EVENT_CHARS = 60

#: Events that (re)register the hosting session through the write path:
#: SessionStart once per session, SessionHeartbeat every 60 s while it
#: lives — the self-heal for a start event kimi never delivered.
_REGISTER_EVENTS = frozenset({"SessionStart", "SessionHeartbeat"})


class HookFailure(StrEnum):
    """Why a hook run did not register a session — the trail's `reason=`."""

    BAD_JSON = "bad-json"
    BAD_PAYLOAD = "bad-payload"
    UNKNOWN_EVENT = "unknown-event"
    NO_SESSION_ID = "no-session-id"
    NO_ANCESTRY = "no-ancestry"
    HOST_UNREADABLE = "host-unreadable"
    WRITE_FAILED = "write-failed"
    REMOVE_FAILED = "remove-failed"
    STRAY_ARGS = "stray-args"


def _kimi_pid() -> int | None:
    """The hosting kimi process: this hook's nearest grandparent pid."""
    shell = proc.read_proc_stat(os.getppid())
    if shell.state is not ProcReadState.AVAILABLE:
        return None
    return shell.ppid


def _event_text(event: object) -> str:
    """One quoted line's worth of an arbitrary payload value — never more.

    `hook_event_name` is attacker-shaped input as far as this file is
    concerned: unbounded text would evict real evidence from a trail that
    keeps only `MAX_ERROR_LINES`, a control character would forge extra
    lines, and bare text could imitate this line's own `reason=`/`pid=`
    fields. Quoting plus a printable-only filter keeps a hostile value
    legible AS a value."""
    if not isinstance(event, str) or not event:
        return '"?"'
    cleaned = "".join(ch if ch.isprintable() and ch != '"' else " " for ch in event)
    return f'"{cleaned[:MAX_EVENT_CHARS]}"'


def record_failure(
    run_dir: Path, reason: HookFailure, event: object = None, pid: int | None = None
) -> None:
    """Append one bounded diagnostic line; never change the hook's outcome.

    The caller is a hook whose exit code is discarded, so a trail that cannot
    be written has no recovery worth taking: OSError (advisory-lock trouble
    included — `AdvisoryLockError` is one) and a failed replace are swallowed
    here, and only here. A log body that is missing or not UTF-8 restarts the
    trail rather than escaping as a traceback. The read-modify-write runs
    under the same advisory lock `curation` uses, so concurrent session
    starts cannot drop the very evidence this exists to keep."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = (
        f"{stamp} reason={reason} event={_event_text(event)} "
        f"pid={pid if pid is not None else '?'}"
    )
    path = run_dir / ERROR_LOG
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        with advisory_lock(path.with_name(f".{ERROR_LOG}.lock")):
            try:
                kept = path.read_text().splitlines()
            except (FileNotFoundError, UnicodeDecodeError):
                kept = []
            kept.append(line)
            atomic_replace(path, "\n".join(kept[-MAX_ERROR_LINES:]) + "\n")
    except (OSError, AtomicWriteError):
        return


def registry_dir() -> Path:
    """Where both the registry and its diagnostic trail live."""
    return cfg.kimi_home / REGISTRY_DIR


def run_hook(payload_text: str) -> int:
    """Apply one hook event payload to the runtime registry."""
    run_dir = registry_dir()
    try:
        payload = json.loads(payload_text)
    except ValueError:
        record_failure(run_dir, HookFailure.BAD_JSON)
        return 2
    if not isinstance(payload, dict):
        record_failure(run_dir, HookFailure.BAD_PAYLOAD)
        return 2
    event = payload.get("hook_event_name")
    if event != "SessionEnd" and event not in _REGISTER_EVENTS:
        record_failure(run_dir, HookFailure.UNKNOWN_EVENT, event)
        return 0
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        record_failure(run_dir, HookFailure.NO_SESSION_ID, event)
        return 2
    pid = _kimi_pid()
    if pid is None:
        record_failure(run_dir, HookFailure.NO_ANCESTRY, event)
        return 3
    path = run_dir / f"{pid}.json"
    if event == "SessionEnd":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            record_failure(run_dir, HookFailure.REMOVE_FAILED, event, pid)
            return 4
        return 0
    stat = proc.read_proc_stat(pid)
    if stat.state is not ProcReadState.AVAILABLE or not stat.starttime:
        record_failure(run_dir, HookFailure.HOST_UNREADABLE, event, pid)
        return 3
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(
            path, json.dumps({"sessionId": sid, "procStart": stat.starttime})
        )
    except (OSError, AtomicWriteError):
        record_failure(run_dir, HookFailure.WRITE_FAILED, event, pid)
        return 4
    prune_gone_entries(run_dir, pid)
    return 0
