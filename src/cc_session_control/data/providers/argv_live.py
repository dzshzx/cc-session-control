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

from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from ..proc import ProcCli

#: PURE per-provider argv matcher: the session id this process is running,
#: or None when the argv does not prove one.
ArgvExtractor = Callable[[tuple[str, ...]], str | None]


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
