"""kimi SessionStart/SessionEnd hook endpoint — the runtime-registry writer.

kimi's official hook seam (verified on 0.34.0, 2026-08-12): a `[[hooks]]`
rule in `~/.kimi-code/config.toml` pipes an event payload (JSON, stdin) to
`csctl _kimi-hook`; this module turns it into the runtime registry the kimi
provider binds from (`data/providers/kimi.py::_read_registry` — the same
pid-keyed shape as Claude's `sessions/<pid>.json`).

- SessionStart (payload: `session_id`, `cwd`, `source` startup|resume;
  fires when the session materializes — a TUI's first prompt, immediately
  for `--prompt`) writes `run/<pid>.json` = {"sessionId", "procStart"}.
- SessionEnd (`reason` exit|archive) removes it.
- Any other event is ignored (forward-compatible with future rules).

The hosting kimi pid is the hook's grandparent (kimi → `sh -c` → this
process — the one-hop shell layer is verified on 0.34.0). The reader side
re-verifies identity (argv0/comm/exe) and starttime per entry, so a wrong or
forged write can never mint a binding — fail closed by construction.

Exit codes: 0 processed / deliberately ignored; 2 malformed payload;
3 hosting-process ancestry unreadable; 4 registry write/remove failed.
"""

from __future__ import annotations

import json
import os

from ..config import cfg
from ..data import proc
from ..data.atomic_write import AtomicWriteError, atomic_replace
from ..data.proc import ProcReadState
from ..data.providers.kimi import REGISTRY_DIR


def _kimi_pid() -> int | None:
    """The hosting kimi process: this hook's nearest grandparent pid."""
    shell = proc.read_proc_stat(os.getppid())
    if shell.state is not ProcReadState.AVAILABLE:
        return None
    return shell.ppid


def run_hook(payload_text: str) -> int:
    """Apply one hook event payload to the runtime registry."""
    try:
        payload = json.loads(payload_text)
    except ValueError:
        return 2
    if not isinstance(payload, dict):
        return 2
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "SessionEnd"):
        return 0
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return 2
    pid = _kimi_pid()
    if pid is None:
        return 3
    path = cfg.kimi_home / REGISTRY_DIR / f"{pid}.json"
    if event == "SessionEnd":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return 4
        return 0
    stat = proc.read_proc_stat(pid)
    if stat.state is not ProcReadState.AVAILABLE or not stat.starttime:
        return 3
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(
            path, json.dumps({"sessionId": sid, "procStart": stat.starttime})
        )
    except (OSError, AtomicWriteError):
        return 4
    return 0
