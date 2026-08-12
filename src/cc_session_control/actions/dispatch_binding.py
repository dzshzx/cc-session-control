"""Late-sid dispatch binding: the `@csctl_sid` backfill watch (kimi).

kimi assigns a session id only when the FIRST PROMPT registers the session
(0.34.0 probes, 2026-08-12: bare startup writes nothing to the index, and
`--session <unknown-id>` refuses to start — csctl can neither mint nor know
the id at spawn), so a csctl-dispatched NEW kimi window cannot declare
`@csctl_sid` the way a resume dispatch can (`tmux._declare_dispatch_metadata`).
`new_session_spawn_cmd` therefore prefixes the spawn command with this
watcher, backgrounded INSIDE the dispatched pane; once the session registers,
the watcher writes `@csctl_sid` onto its own window — the same option, from
the same csctl authority — completing the C1 metadata binding (the title
rewrite then stays irrelevant). Until the write lands, the row keeps the
unbound-live hint, which is exactly the pre-watch behavior.

Safety discipline (same as `argv_live.build_metadata_index` — every doubt
binds nothing): the candidate sid must be ABSENT from the watcher's
spawn-time index snapshot and UNIQUE for the dispatch directory; the pane
process identity captured at watcher start (pid + /proc starttime) must
still hold at write time; any ambiguity or evidence loss exits without a
write. The residual uncovered shape — a FOREIGN kimi registering the only
new session in the same directory while the dispatched one stays
prompt-less — is indistinguishable in principle (kimi records no
pid↔session evidence); its blast radius is a SIGTERM to an empty fresh REPL.

Exit codes: 0 bound; 2 several candidate sessions (ambiguous); 3 pane/window
evidence lost or absent; 4 pane process identity unverifiable or changed;
5 the option write failed; 6 unsupported provider; 7 horizon exhausted.
Every non-zero exit leaves the window unbound — never a wrong binding.
"""

from __future__ import annotations

import os
import shlex
import shutil
import time
from collections.abc import Callable

from ..data import proc, tmux
from ..data.providers import kimi
from ..data.providers.base import AgentProvider

_INTERVAL = 3.0
# A dispatched window may sit idle for hours before its first prompt.
_HORIZON = 12 * 3600.0
_LIVENESS_EVERY = 20  # loops between pane-liveness probes


def new_session_spawn_cmd(directory: str, provider: AgentProvider) -> str:
    """The tmux-window shell command starting a fresh `provider` session.

    Plain `cd && <argv>` — except late-sid providers (kimi), whose command
    also backgrounds the binding watch and `exec`s the CLI so the pane pid
    stays the CLI process (the metadata join's root-process shortcut). No
    resolvable `csctl` on PATH degrades to the plain command: the window
    simply stays unbound."""
    line = shlex.join(provider.new_session_argv())
    plain = f"cd {shlex.quote(directory)} && {line}"
    if not provider.caps.late_sid:
        return plain
    watcher = shutil.which("csctl")
    if watcher is None:
        return plain
    bind = shlex.join([watcher, "_bind-window", provider.key, directory])
    return f"{bind} >/dev/null 2>&1 & cd {shlex.quote(directory)} && exec {line}"


def run_binding_watch(
    provider_key: str,
    directory: str,
    *,
    interval: float = _INTERVAL,
    horizon: float = _HORIZON,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Diff the provider's session index and backfill this window's sid.

    Runs backgrounded inside the dispatched tmux pane (`$TMUX_PANE`); the
    module docstring holds the exit-code contract. `sleep`/`monotonic` are
    test seams; all other IO goes through `data/` (`tmux`, `proc`, `kimi`).
    """
    if provider_key != kimi.KimiProvider.key:
        return 6
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return 3
    identity = tmux.pane_window_identity(pane)
    if identity is None:
        return 3
    window_id, pane_pid = identity
    probe = proc.probe_pid(pane_pid, None)
    starttime = probe.stat.starttime if probe.stat is not None else None
    if not probe.alive or not starttime:
        return 4
    prior: frozenset[str] | None = None
    deadline = monotonic() + horizon
    loops = 0
    while monotonic() < deadline:
        sleep(interval)
        if prior is None:
            # The snapshot MUST predate registration: a late base would
            # misread pre-existing rows as newly created by this window.
            prior = kimi.index_sids()
            continue
        loops += 1
        if loops % _LIVENESS_EVERY == 0 and tmux.pane_window_identity(pane) is None:
            return 3
        candidates = kimi.new_sids_since(prior, directory)
        if candidates is None:
            continue
        if len(candidates) > 1:
            return 2
        if not candidates:
            continue
        if not proc.probe_pid(pane_pid, starttime).alive:
            return 4
        return 0 if tmux.declare_dispatch_sid(window_id, candidates[0]).success else 5
    return 7
