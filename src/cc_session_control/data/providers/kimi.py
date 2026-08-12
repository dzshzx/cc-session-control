"""Kimi Code provider — disk discovery + argv-exact liveness (ADR-0005).

Upstream contracts (verified on Kimi Code 0.31.1, re-verified on 0.34.0
2026-08-12, re-verify per release): `$KIMI_CODE_HOME/session_index.jsonl`
maps `sessionId` → `sessionDir` / `workDir`; each session dir holds
`state.json` (`title`, `lastPrompt`, `workDir`, `updatedAt` — metadata only)
and `agents/main/wire.jsonl`, the main agent's actual conversation log
(subagents get their own `agents/agent-N/wire.jsonl`, not covered here).
`Session.file` points at the wire log so headless resume's transcript-body
search fallback has real content to search; `Session.mtime` still comes from
`state.json`'s stat, not the wire log's. Resume: `kimi --session <sid>`
(short `-S`); fork exists only as the in-session `/fork`, so `caps.fork` is
False and a fork argv request is a programming error.

A bare `kimi` REPL leaves no STABLE pid↔session evidence: no env var
(0.31.1 + 0.34.0 probed), and the wire-log fd 0.34.0 holds is transient
(open around writes only — scan-time evidence would flap). The title
rewrite destroys even a dispatched session's `--session` argv within moments
of start (C1; 0.31.1 collapsed cmdline to `kimi-code`, 0.34.0 to bare
`kimi`) — so bindings (the only kill targets) come from `--session` argv
matches where the argv survives, plus csctl's own
`@csctl_sid`/`@csctl_provider` window metadata for dispatched windows,
identity-checked via argv0/comm/exe (`is_provider_process`).

New-session dispatches have a late-sid gap (0.34.0 probes): the index entry
is written at the FIRST PROMPT, not at startup, and `--session
<unknown-id>` refuses to start, so csctl can neither mint the sid nor know
it at spawn. The covered fix is the opt-in runtime registry below (the
CLI's official hooks self-report pid↔sid for ANY session — bare or
dispatched); without it, dispatched new sessions fall back to the
unbound-live hint until a later resume declares the sid.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from collections.abc import Set as AbstractSet

from ...config import cfg
from ...models import InventoryIssue, Session
from .. import proc
from ..proc import ProcCli, ProcCliInventory
from ..tmux_outcomes import PaneInventory
from .argv_live import (
    ArgvMatch,
    apply_unbound_hints,
    bound_pids,
    build_live_index,
    flag_value,
    unbound_live_cwds,
)
from .base import LivenessGrade, ProviderCaps, ProviderScan

BASENAME = "kimi"
# The comm/argv0 an active kimi 0.31.1 process rewrites itself to (observed
# 2026-08-04: cmdline collapses to `kimi-code` + padding, comm follows).
# 0.34.0 collapses to bare `kimi` instead (observed 2026-08-12) — keep both
# capture shapes; per-record identity is re-verified either way (C1).
TITLE_COMM = "kimi-code"

# Non-interactive shapes from `kimi --help` (verified 0.31.x-line, this
# machine, 2026-08-04): servers/utility subcommands plus the one-shot
# `--prompt` print mode never hold an interactive REPL, so they must not
# feed the unbound-live hint. The bare REPL, `--continue`, and the `-S`
# picker (no id) DO — they are exactly the unbindable shapes ADR-0005
# documents.
_NON_TUI_SUBCOMMANDS = frozenset(
    {
        "export",
        "provider",
        "acp",
        "web",
        "server",
        "login",
        "doctor",
        "vis",
        "migrate",
        "upgrade",
        "update",
        "help",
    }
)
_NON_TUI_FLAGS = frozenset({"-p", "--prompt", "-V", "--version", "-h", "--help"})


def extract_sid(argv: tuple[str, ...]) -> str | None:
    """PURE: the sid a kimi process argv proves it is running."""
    if len(argv) < 2 or os.path.basename(argv[0]) != BASENAME:
        return None
    value = flag_value(argv[1:], "--session", "-S")
    if value and not value.startswith("-"):
        return value
    return None


def is_provider_process(record: ProcCli) -> bool:
    """PURE: Kimi Code process identity (C1). argv0 basename alone stopped
    sufficing on 0.31.1 — the runtime rewrites its own title, collapsing
    cmdline to `kimi-code`; comm follows the rewrite while exe still points
    at the real binary — so any of argv0 `kimi`, comm `kimi-code`, or exe
    basename `kimi` marks identity (an unreadable comm/exe is "" and simply
    loses that alternative)."""
    if record.argv and os.path.basename(record.argv[0]) == BASENAME:
        return True
    if record.comm == TITLE_COMM:
        return True
    return bool(record.exe) and os.path.basename(record.exe) == BASENAME


def is_tui_process(record: ProcCli) -> bool:
    """PURE: session-holding interactive kimi REPL — process identity AND a
    non-daemon argv shape. Feeds the unbound-live hint and metadata-binding
    candidacy, never liveness by itself. Same conservative token matching as
    the codex twin: a false denylist hit only costs a missed hint/binding,
    never a server entering either source."""
    return is_provider_process(record) and not any(
        tok in _NON_TUI_SUBCOMMANDS or tok in _NON_TUI_FLAGS for tok in record.argv[1:]
    )


def _issue(path: str, detail: str) -> InventoryIssue:
    return InventoryIssue("kimi sessions", path, detail)


def _read_state(
    session_dir: str,
) -> tuple[dict | None, InventoryIssue | None]:
    """`state.json` of one session dir as (state, degradation evidence).

    A MISSING file degrades silently (the append-only index outlives deleted
    session dirs — flagging every historical gap would degrade forever); an
    unreadable or malformed one surfaces as an issue (AGENTS.md 外部失败)."""
    path = os.path.join(session_dir, "state.json")
    try:
        with open(path, "rb") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, _issue(path, str(exc))
    if not isinstance(state, dict):
        return None, _issue(path, "state.json is not a JSON object")
    return state, None


def _read_index(path: str) -> tuple[dict[str, dict], InventoryIssue | None]:
    """The append-only session index as sid → entry, plus read-failure evidence.

    A MISSING index is not an issue (a fresh install has no state — callers
    get an empty mapping); torn tail lines of the append-only file are
    skipped; an unreadable index surfaces as an issue (AGENTS.md 外部失败)."""
    entries: dict[str, dict] = {}
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"sessionId"' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue  # torn tail line of an append-only index
                sid = entry.get("sessionId")
                if isinstance(sid, str) and sid:
                    entries[sid] = entry  # last write wins
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, _issue(path, str(exc))
    return entries, None


# --- runtime registry (opt-in, hook-maintained pid↔session evidence) --------
#
# kimi's official SessionStart/SessionEnd hooks run `csctl _kimi-hook`
# (actions/kimi_hook), maintaining one `<pid>.json` (`sessionId` +
# `procStart`) per live session in this directory — the CLI's OWN
# self-report, the same shape csctl consumes for Claude
# (`sessions/<pid>.json`). Hook contract verified on 0.34.0 (2026-08-12):
# SessionStart fires when the session materializes (a TUI's first prompt;
# immediately for `--prompt`; `source` is startup|resume) carrying
# `session_id`/`cwd`; SessionEnd fires on a clean exit; the hook process is
# kimi's grandchild (kimi → sh → hook). SIGKILL leaves a stale file — the
# reader's starttime recheck fails closed on pid reuse.

#: Registry directory name under the kimi home — part of the provider's
#: contract surface, also written by `actions/kimi_hook`.
REGISTRY_DIR = "run"


def _read_registry() -> tuple[dict[int, tuple[str, str]], tuple[InventoryIssue, ...]]:
    """The runtime registry as pid → (sessionId, procStart), plus issues.

    A missing directory is not an issue (the hook is opt-in — no config, no
    registry, not a failure); an unreadable or malformed entry surfaces as an
    issue (AGENTS.md 外部失败)."""
    registry_dir = cfg.kimi_home / REGISTRY_DIR
    entries: dict[int, tuple[str, str]] = {}
    issues: list[InventoryIssue] = []
    try:
        files = sorted(registry_dir.glob("*.json"))
    except OSError as exc:
        return {}, (_issue(os.fspath(registry_dir), str(exc)),)
    for path in files:
        name = path.name[: -len(".json")]
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            issues.append(_issue(os.fspath(path), str(exc)))
            continue
        sid = record.get("sessionId") if isinstance(record, dict) else None
        proc_start = record.get("procStart") if isinstance(record, dict) else None
        if (
            not name.isdigit()
            or not isinstance(sid, str)
            or not sid
            or not isinstance(proc_start, str)
            or not proc_start
        ):
            issues.append(_issue(os.fspath(path), "malformed registry entry"))
            continue
        entries[int(name)] = (sid, proc_start)
    return entries, tuple(issues)


def _registry_index(
    entries: dict[int, tuple[str, str]],
    records: Iterable[ProcCli],
    cur: AbstractSet[int],
) -> dict[str, ArgvMatch]:
    """PURE: registry entries → bindings, re-verified against the /proc walk.

    An entry binds only when the walk netted the pid, the record passes
    `is_provider_process` (argv0/comm/exe), and the scan-time starttime
    matches the recorded procStart — a stale (crashed) or forged file never
    binds. One sid claimed by two live pids (double-attach) binds nobody: a
    disputed kill target is never a coin-flip pick (the metadata join's
    ambiguity rule)."""
    by_pid = {record.pid: record for record in records}
    valid: dict[str, tuple[int, str]] = {}
    claiming: dict[str, set[int]] = {}
    for pid, (sid, proc_start) in entries.items():
        record = by_pid.get(pid)
        if (
            record is None
            or not is_provider_process(record)
            or record.starttime != proc_start
        ):
            continue
        valid.setdefault(sid, (pid, proc_start))
        claiming.setdefault(sid, set()).add(pid)
    return {
        sid: ArgvMatch(sid=sid, pid=pid, proc_start=proc_start, current=pid in cur)
        for sid, (pid, proc_start) in valid.items()
        if len(claiming[sid]) == 1
    }


class KimiProvider:
    key = "kimi"
    label = "km"
    basename = BASENAME
    # The /proc walk must also net title-rewritten processes (argv0 becomes
    # `kimi-code`) — identity is then re-verified per record (C1).
    capture_basenames = frozenset({BASENAME, TITLE_COMM})
    caps = ProviderCaps(
        # argv-exact + runtime-registry + dispatch-metadata matches only
        takeover=True,
        liveness=LivenessGrade.TMUX,  # the title rewrite destroys argv evidence
    )

    def available(self) -> bool:
        # Home existence, per ADR-0005 — a fresh install with zero sessions
        # must still activate (launcher `k`); discover() tolerates the
        # missing index.
        return cfg.kimi_home.is_dir()

    def resume_argv(self, sid: str, fork: bool = False) -> list[str]:
        if fork:
            raise ValueError("kimi has no CLI fork (in-session /fork only)")
        return ["kimi", "--session", sid]

    def new_session_argv(self) -> list[str]:
        return ["kimi"]

    def window_name(self, sid: str, fork: bool = False) -> str:
        short = sid.removeprefix("session_")[:8]
        return f"km-{short}"

    def discover(
        self,
        cli_inventory: ProcCliInventory,
        cur: AbstractSet[int],
        panes: PaneInventory | None = None,
    ) -> ProviderScan:
        issues: list[InventoryIssue] = []
        registry, registry_issues = _read_registry()
        issues.extend(registry_issues)
        live = build_live_index(
            cli_inventory.records,
            extract_sid,
            cur,
            panes=panes,
            provider_key=self.key,
            is_tui_process=is_tui_process,
            ancestors_of=proc.probe_ancestors,
            registry=_registry_index(registry, cli_inventory.records, cur),
        )
        index_path = cfg.kimi_home / "session_index.jsonl"
        entries, index_issue = _read_index(os.fspath(index_path))
        if index_issue is not None:
            return ProviderScan(issues=(index_issue,))

        rows = apply_unbound_hints(
            (self._project(sid, entry, live, issues) for sid, entry in entries.items()),
            unbound_live_cwds(
                cli_inventory.records,
                extract_sid,
                is_tui_process,
                bound_pids(live),
            ),
        )
        return ProviderScan(rows, tuple(issues))

    def _project(
        self,
        sid: str,
        entry: dict,
        live: dict,
        issues: list[InventoryIssue],
    ) -> Session:
        session_dir = entry.get("sessionDir")
        state = None
        if isinstance(session_dir, str):
            state, state_issue = _read_state(session_dir)
            if state_issue is not None:
                issues.append(state_issue)
        mtime = 0.0
        if isinstance(session_dir, str):
            try:
                mtime = os.stat(os.path.join(session_dir, "state.json")).st_mtime
            except OSError:
                mtime = 0.0
        label = "(untitled)"
        index_work_dir = entry.get("workDir")
        cwd = index_work_dir if isinstance(index_work_dir, str) else ""
        if state is not None:
            title = state.get("title")
            last_prompt = state.get("lastPrompt")
            label = (
                title
                if isinstance(title, str) and title
                else last_prompt
                if isinstance(last_prompt, str) and last_prompt
                else label
            )
            work_dir = state.get("workDir")
            if isinstance(work_dir, str) and work_dir:
                cwd = work_dir
        match = live.get(sid)
        return Session(
            sid=sid,
            cwd=cwd,
            label=label,
            mtime=mtime,
            prompts=0,
            pid=match.pid if match else None,
            alive=match is not None,
            current=bool(match and match.current),
            provider=self.key,
            proc_start=match.proc_start if match else "",
            # state.json is metadata only (title/lastPrompt/workDir); the
            # actual conversation body — what headless resume's transcript
            # fallback search needs — lives in the main agent's wire log.
            file=os.path.join(session_dir, "agents", "main", "wire.jsonl")
            if isinstance(session_dir, str)
            else "",
            source="cli",
        )
