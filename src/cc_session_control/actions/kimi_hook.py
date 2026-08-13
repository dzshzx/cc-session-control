"""kimi SessionStart/SessionEnd hook endpoint — the runtime-registry writer.

kimi's official hook seam (verified on 0.34.0 2026-08-12, re-verified on
0.35.0 2026-08-13): a `[[hooks]]` rule in `~/.kimi-code/config.toml` pipes
an event payload (JSON, stdin) to `csctl _kimi-hook`; this module turns it
into the runtime registry the kimi provider binds from
(`data/providers/kimi.py::_read_registry` — the same pid-keyed shape as
Claude's `sessions/<pid>.json`). The payload keys are snake_case: 0.35.0
builds them camelCase and runs `toHookInputData`/`camelToSnake` before
spawning the command, so `hook_event_name`/`session_id` stay the contract.

- SessionStart (payload: `session_id`, `cwd`, `source` startup|resume;
  fires when the session materializes — a TUI's first prompt, immediately
  for `--prompt`; both paths re-verified on 0.35.0) writes `run/<pid>.json`
  = {"sessionId", "procStart"}.
- SessionEnd removes it — but do NOT count on it: 0.35.0 left the entry in
  place after a clean `kimi -p` exit and after a killed TUI (2026-08-13),
  so entries accumulate. `_prune_gone` is what actually bounds the
  directory; the reader's starttime recheck is what keeps leftovers inert.
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
from pathlib import Path

from ..config import cfg
from ..data import proc
from ..data.atomic_write import AtomicWriteError, atomic_replace
from ..data.proc import ProcReadState
from ..data.providers.kimi import REGISTRY_DIR

#: Diagnostic trail for hook runs that did not register a session, kept next
#: to the registry it explains. Bounded: the newest lines win.
ERROR_LOG = "hook-errors.log"
MAX_ERROR_LINES = 50


def _kimi_pid() -> int | None:
    """The hosting kimi process: this hook's nearest grandparent pid."""
    shell = proc.read_proc_stat(os.getppid())
    if shell.state is not ProcReadState.AVAILABLE:
        return None
    return shell.ppid


def _record_failure(run_dir: Path, reason: str, event: object, pid: int | None) -> None:
    """Append one bounded diagnostic line; never change the hook's outcome.

    A failed write here has no recovery worth taking — the caller is a hook
    whose exit code is discarded — so OSError is swallowed deliberately, and
    only here."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    event_text = event if isinstance(event, str) and event else "?"
    line = (
        f"{stamp} reason={reason} event={event_text} "
        f"pid={pid if pid is not None else '?'}"
    )
    path = run_dir / ERROR_LOG
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            kept = path.read_text().splitlines()
        except FileNotFoundError:
            kept = []
        kept.append(line)
        atomic_replace(path, "\n".join(kept[-MAX_ERROR_LINES:]) + "\n")
    except (OSError, AtomicWriteError):
        return


def _prune_gone(run_dir: Path, keep_pid: int) -> None:
    """Drop entries whose pid is PROVABLY gone (SessionEnd is unreliable).

    R10: only `GONE` qualifies — `UNAVAILABLE` (no `/proc`) and `MALFORMED`
    leave the entry alone, so a platform without `/proc` never empties the
    registry it cannot judge."""
    try:
        entries = sorted(run_dir.glob("*.json"))
    except OSError:
        return
    for entry in entries:
        name = entry.name[: -len(".json")]
        if not name.isdigit() or int(name) == keep_pid:
            continue
        if proc.read_proc_stat(int(name)).state is not ProcReadState.GONE:
            continue
        try:
            entry.unlink(missing_ok=True)
        except OSError:
            continue


def run_hook(payload_text: str) -> int:
    """Apply one hook event payload to the runtime registry."""
    run_dir = cfg.kimi_home / REGISTRY_DIR
    try:
        payload = json.loads(payload_text)
    except ValueError:
        _record_failure(run_dir, "bad-json", None, None)
        return 2
    if not isinstance(payload, dict):
        _record_failure(run_dir, "bad-payload", None, None)
        return 2
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "SessionEnd"):
        _record_failure(run_dir, "unknown-event", event, None)
        return 0
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        _record_failure(run_dir, "no-session-id", event, None)
        return 2
    pid = _kimi_pid()
    if pid is None:
        _record_failure(run_dir, "no-ancestry", event, None)
        return 3
    path = run_dir / f"{pid}.json"
    if event == "SessionEnd":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            _record_failure(run_dir, "remove-failed", event, pid)
            return 4
        return 0
    stat = proc.read_proc_stat(pid)
    if stat.state is not ProcReadState.AVAILABLE or not stat.starttime:
        _record_failure(run_dir, "host-unreadable", event, pid)
        return 3
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(
            path, json.dumps({"sessionId": sid, "procStart": stat.starttime})
        )
    except (OSError, AtomicWriteError):
        _record_failure(run_dir, "write-failed", event, pid)
        return 4
    _prune_gone(run_dir, pid)
    return 0
