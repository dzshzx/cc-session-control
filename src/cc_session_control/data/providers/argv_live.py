"""Argv-exact liveness shared by non-Claude providers (ADR-0005).

Codex/kimi have no registry equivalent, so their only takeover-grade
pid↔session evidence is a process argv that CARRIES the session id
(`codex resume <sid>` / `kimi --session <sid>`). csctl's own tmux dispatch
always resumes by id, so every workbench-managed session is exactly
matchable; bare TUIs (argv without a sid) stay unbound and are never kill
targets. Each provider supplies a PURE `extract(argv) -> sid | None`; the
join here is IO-free — `data.proc.scan_cli_argv_inventory` provides the one
walk per generation.
"""

from __future__ import annotations

import os.path
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace

from ...models import Session
from ..proc import ProcCli

#: PURE per-provider argv matcher: the session id this process is running,
#: or None when the argv does not prove one.
ArgvExtractor = Callable[[tuple[str, ...]], str | None]

#: PURE per-provider argv-shape predicate: does this argv look like a
#: session-holding interactive TUI of the provider's CLI? Daemons and
#: utility subcommands answer False — they are not "unbound TUIs" and must
#: never feed the unbound-live hint.
TuiShapePredicate = Callable[[tuple[str, ...]], bool]


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


def unbound_live_cwds(
    records: Iterable[ProcCli],
    extract: ArgvExtractor,
    is_tui_shape: TuiShapePredicate,
) -> frozenset[str]:
    """PURE: normalized cwds of live TUI-shaped processes whose argv proves
    NO sid — the unbound-live hint source (the fourth status state).

    A record whose cwd could not be read (empty) is silently absent, and
    daemon/utility shapes never enter. Downstream this set only marks rows
    as *possibly* held; it never contributes liveness.
    """
    return frozenset(
        os.path.normpath(record.cwd)
        for record in records
        if record.cwd and extract(record.argv) is None and is_tui_shape(record.argv)
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
