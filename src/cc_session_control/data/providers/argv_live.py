"""Non-Claude pid↔session liveness evidence (ADR-0005 + C1 amendment).

Codex/kimi have no built-in registry equivalent. Takeover-grade sources join
a pid to a session id, argv first:

- **argv-exact**: a process argv that CARRIES the session id
  (`codex resume <sid>` / `kimi --session <sid>`).
- **runtime registry** (kimi, opt-in): kimi's official SessionStart/SessionEnd
  hooks run `csctl _kimi-hook`, which maintains `run/<pid>.json`
  (`sessionId` + `procStart`) under the kimi home — the CLI's own
  self-report, the same shape as Claude's `sessions/<pid>.json`, validated
  against the /proc walk's starttime so a stale or forged entry never binds.
- **tmux dispatch metadata** (supplement): kimi rewrites its own process
  title at runtime, destroying that argv evidence for the very sessions
  csctl dispatched — so csctl's spawns declare `@csctl_sid`/`@csctl_provider`
  window options and `build_metadata_index` joins a declaring pane back to
  the pane process that matches the provider's process-identity set.

Bare TUIs without registry or metadata evidence stay unbound and are never
kill targets. Each provider supplies PURE predicates; `build_argv_index`
stays IO-free, and the metadata join's only IO is the injected ancestor
prober (`data.proc.scan_cli_argv_inventory` provides the one walk per
generation, `data.tmux.list_panes_inventory` the one pane walk).
"""

from __future__ import annotations

import os.path
from collections.abc import Callable, Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace

from ...models import Session
from ..proc import AncestorProbe, ProcCli
from ..tmux_outcomes import PaneInventory

#: PURE per-provider argv matcher: the session id this process is running,
#: or None when the argv does not prove one.
ArgvExtractor = Callable[[tuple[str, ...]], str | None]

#: PURE per-provider process predicate: does this /proc record look like a
#: session-holding interactive TUI of the provider's CLI (process identity
#: via argv0/comm/exe PLUS a non-daemon argv shape)? Daemons and utility
#: subcommands answer False — they are not "unbound TUIs" and must never
#: feed the unbound-live hint nor become metadata-binding candidates.
TuiProcessPredicate = Callable[[ProcCli], bool]

#: Targeted-IO ancestor prober the metadata join receives (production:
#: `proc.probe_ancestors`; tests inject pure fakes).
AncestorProber = Callable[[int], AncestorProbe]


@dataclass(frozen=True)
class ArgvMatch:
    """One argv-proven binding of a live pid to a session id."""

    sid: str
    pid: int
    proc_start: str
    current: bool = False


def build_argv_index(
    records: Iterable[ProcCli],
    extract: ArgvExtractor,
    cur: AbstractSet[int],
) -> dict[str, ArgvMatch]:
    """PURE: join scanned CLI processes to session ids via `extract`.

    Later records win on a duplicate sid (should not happen — two processes
    resuming one session — but a deterministic rule beats an assertion on
    external state). `cur` is the csctl ancestor pid set: an argv-matched
    ancestor makes that session "current" and therefore protected.
    """
    index: dict[str, ArgvMatch] = {}
    for record in records:
        sid = extract(record.argv)
        if not sid:
            continue
        index[sid] = ArgvMatch(
            sid=sid,
            pid=record.pid,
            proc_start=record.starttime,
            current=record.pid in cur,
        )
    return index


def _pane_tui_process(
    pane_pid: int,
    candidates: Sequence[ProcCli],
    ancestors_of: AncestorProber,
) -> ProcCli | None:
    """The one candidate a declaring pane proves: its root process, else its
    UNIQUE TUI-shaped descendant (the spawn shell may wrap the CLI). Zero or
    several descendants prove nothing (fail closed)."""
    for record in candidates:
        if record.pid == pane_pid:
            return record
    under = [r for r in candidates if pane_pid in ancestors_of(r.pid).pids]
    return under[0] if len(under) == 1 else None


def build_metadata_index(
    panes: PaneInventory | None,
    provider_key: str,
    records: Iterable[ProcCli],
    is_tui_process: TuiProcessPredicate,
    argv_bound_pids: AbstractSet[int],
    cur: AbstractSet[int],
    ancestors_of: AncestorProber,
) -> dict[str, ArgvMatch]:
    """sid → binding proven by csctl's OWN dispatch metadata (C1).

    A pane whose window carries `@csctl_sid` + a matching `@csctl_provider`
    binds the sid to the pane process `_pane_tui_process` identifies —
    `proc_start` is the walk's scan-time capture, so the kill-time
    `probe_pid` recheck defeats pid reuse exactly like argv matches. Every
    doubt binds nothing: no panes / an incomplete pane inventory, a missing
    option, an identity or shape mismatch, and a vanished pane process all
    fail closed; so does ambiguity in EITHER direction — a sid claimed by
    panes over distinct pids, and a pid claimed by panes for distinct sids
    (one process runs one session; a disputed pid binds nobody, never a
    coin-flip pick). Argv-bound pids are excluded so metadata can never
    re-bind proven processes; window names never participate.
    """
    if panes is None or not panes.complete:
        return {}
    candidates = tuple(
        r for r in records if r.pid not in argv_bound_pids and is_tui_process(r)
    )
    if not candidates:
        return {}
    bound: dict[str, ProcCli] = {}
    ambiguous: set[str] = set()
    sids_claiming_pid: dict[int, set[str]] = {}
    for pane in panes.records:
        if pane.provider != provider_key or not pane.sid:
            continue
        record = _pane_tui_process(pane.pid, candidates, ancestors_of)
        if record is None:
            continue
        sids_claiming_pid.setdefault(record.pid, set()).add(pane.sid)
        if bound.setdefault(pane.sid, record).pid != record.pid:
            ambiguous.add(pane.sid)
    return {
        sid: ArgvMatch(
            sid=sid,
            pid=record.pid,
            proc_start=record.starttime,
            current=record.pid in cur,
        )
        for sid, record in bound.items()
        if sid not in ambiguous and len(sids_claiming_pid[record.pid]) == 1
    }


def build_live_index(
    records: Iterable[ProcCli],
    extract: ArgvExtractor,
    cur: AbstractSet[int],
    *,
    panes: PaneInventory | None,
    provider_key: str,
    is_tui_process: TuiProcessPredicate,
    ancestors_of: AncestorProber,
    registry: dict[str, ArgvMatch] | None = None,
) -> dict[str, ArgvMatch]:
    """THE combined sid → binding view both providers' `discover` consumes:
    argv-exact bindings first, then a provider runtime registry when one
    exists (kimi's hook-maintained `run/<pid>.json` — the CLI's own
    self-report, stronger than dispatch metadata), then dispatch-metadata
    bindings as the last supplement (a sid already proved is never
    overridden — codex with intact argv is byte-identical to the pre-C1
    behavior)."""
    records = tuple(records)
    live = build_argv_index(records, extract, cur)
    for sid, match in (registry or {}).items():
        live.setdefault(sid, match)
    metadata = build_metadata_index(
        panes,
        provider_key,
        records,
        is_tui_process,
        bound_pids(live),
        cur,
        ancestors_of,
    )
    for sid, match in metadata.items():
        live.setdefault(sid, match)
    return live


def bound_pids(index: dict[str, ArgvMatch]) -> frozenset[int]:
    """PURE: the pids a live index accounts for (hint exclusion input)."""
    return frozenset(match.pid for match in index.values())


def unbound_live_cwds(
    records: Iterable[ProcCli],
    extract: ArgvExtractor,
    is_tui_process: TuiProcessPredicate,
    bound: AbstractSet[int] = frozenset(),
) -> frozenset[str]:
    """PURE: normalized cwds of live TUI processes whose argv proves NO sid
    and whose pid no binding accounts for — the unbound-live hint source
    (the fourth status state).

    A record whose cwd could not be read (empty) is silently absent;
    daemon/utility shapes never enter; and a pid in `bound` (argv- OR
    metadata-bound) is accounted for, so it stops hinting sibling rows.
    Downstream this set only marks rows as *possibly* held; it never
    contributes liveness.
    """
    return frozenset(
        os.path.normpath(record.cwd)
        for record in records
        if record.cwd
        and record.pid not in bound
        and extract(record.argv) is None
        and is_tui_process(record)
    )


def apply_unbound_hints(
    rows: Iterable[Session],
    hint_cwds: AbstractSet[str],
) -> tuple[Session, ...]:
    """PURE: flag, per hint cwd, the newest-mtime NOT-argv-bound row as
    possibly held by an unbound live process (`Session.unbound_live_hint`).

    One process holds one session, so the newest non-alive row in that
    directory is the best guess ("可能" wording downstream) — older siblings
    and argv-bound alive rows stay unflagged. The flag is honest uncertainty
    for the UI/confirm layer only: `alive` is untouched, so stop/takeover/
    kill semantics cannot change (fail-safe). `hint_cwds` comes normalized
    from `unbound_live_cwds`; rows are one provider's, so sids are unique.
    """
    rows = tuple(rows)
    if not hint_cwds:
        return rows
    newest: dict[str, Session] = {}
    for row in rows:
        if row.alive or not row.cwd:
            continue
        cwd = os.path.normpath(row.cwd)
        if cwd not in hint_cwds:
            continue
        kept = newest.get(cwd)
        if kept is None or row.mtime > kept.mtime:
            newest[cwd] = row
    flagged = {row.sid for row in newest.values()}
    if not flagged:
        return rows
    return tuple(
        replace(row, unbound_live_hint=True) if row.sid in flagged else row
        for row in rows
    )


def flag_value(argv: tuple[str, ...], *flags: str) -> str | None:
    """PURE: the value of the first present `--flag value` / `--flag=value`.

    Shared by provider extractors; returns None when absent or valueless.
    """
    for flag in flags:
        prefix = flag + "="
        for i, tok in enumerate(argv):
            if tok == flag:
                if i + 1 < len(argv):
                    return argv[i + 1]
                return None
            if tok.startswith(prefix):
                return tok[len(prefix) :] or None
    return None
