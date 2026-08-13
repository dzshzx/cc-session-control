"""Codex CLI provider — disk discovery + argv-exact liveness (ADR-0005).

Upstream contracts (verified on Codex CLI 0.146.0, re-verify per release):
sessions are NDJSON rollouts at
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` whose FIRST line
is a `session_meta` record (`payload.id` == `payload.session_id` for
top-level rollouts; `thread_source: "subagent"` marks internal subagent
rollouts, skipped here). `session_index.jsonl` maps `id` → `thread_name`.
Resume/fork: `codex resume <sid|name>` (the positional target is "Session id
(UUID) or session name. UUIDs take precedence if it parses" — `codex resume
--help`) / `codex fork <sid>`. Discovery reads
rollout first lines only — never whole files — and covers BOTH the active
date tree and `$CODEX_HOME/archived_sessions/`, a FLAT directory (no date
subtree) that `codex archive <SESSION>` moves rollouts into: those rows carry
`Session.archived` and the resume family hands back `codex unarchive <sid>`
instead of gambling an unverified direct resume. zstd-compressed old rollouts
(`.jsonl.zst`) stay out of scope.

Label fallback: `session_index.jsonl` barely overlaps active rollouts in
practice, so when it has no `thread_name` for a sid, discovery does a
bounded continuation read of the rollout body (still never the whole file —
see `codex_rollout.first_user_message`) looking for the first real `user_message` event
to use as the label.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from pathlib import Path

from ...config import cfg, codex_default_home
from ...models import InventoryIssue, Session
from .. import proc
from ..proc import ProcCli, ProcCliInventory
from ..tmux_outcomes import PaneInventory
from .argv_live import (
    ArgvExtractor,
    ArgvMatch,
    apply_unbound_hints,
    bound_pids,
    build_live_index,
    unbound_live_cwds,
)
from .base import (
    CliDeleteResult,
    CliDeleteStage,
    CliDeleteState,
    LivenessGrade,
    ProviderCaps,
    ProviderScan,
    TrustScan,
)
from .codex_rollout import FIRST_LINE_CAP, first_user_message, read_meta
from .codex_source import classify_source
from .codex_trust import read_trusted_dirs

BASENAME = "codex"

# Subcommands from `codex --help` (verified 0.146.0-line, this machine,
# 2026-08-04) that never hold an interactive session: headless runs,
# daemons/servers, and store/config utilities. `resume`/`fork` are absent on
# purpose — they ARE session-holding TUIs, like the bare `codex [PROMPT]`
# form. "proxy" is kept from the ADR-0005 daemon list even though current
# help no longer shows it (an extra denylist token only costs a missed hint).
_NON_TUI_SUBCOMMANDS = frozenset(
    {
        "exec",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "mcp-server",
        "app-server",
        "remote-control",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "archive",
        "delete",
        "unarchive",
        "exec-server",
        "cloud",
        "features",
        "help",
        "proxy",
    }
)

_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Bounded `codex delete` invocation (same bounded-run + typed-outcome
# discipline as tmux's `_tmux_run_result`); 10s matches the liveness seam's
# `claude agents --json` bound — a store operation that has not returned by
# then is a hang, not work.
_DELETE_TIMEOUT_SECONDS = 10
# Only the TAIL of a failing delete's output enters the typed detail: the
# last chars carry the actual error line, and notices must stay one line.
_DELETE_DETAIL_TAIL_CHARS = 200


def _output_tail(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    if len(text) > _DELETE_DETAIL_TAIL_CHARS:
        return "…" + text[-_DELETE_DETAIL_TAIL_CHARS:]
    return text


def extract_resume_target(argv: tuple[str, ...]) -> str | None:
    """PURE: the raw resume-target token (UUID or thread name) this codex
    argv proves, or None when it is not a real `resume` invocation.

    Grammar per `codex resume --help` (0.146.0): `codex resume [OPTIONS]
    [SESSION_ID] [PROMPT]` — the target is ONLY the token immediately after
    the first `resume` token. Pre-resume global flags (`codex --cd x resume
    <target>`) are fine, but an `exec` token before `resume` means "resume"
    is prompt text, not a subcommand: binding a headless `codex exec` run to
    a sid its prompt merely mentions would make it a wrong SIGTERM target.
    A missing or flag-shaped target (`codex resume --last`, the bare picker)
    binds nothing; flags are never skipped to hunt for a target, since
    options take values (`-c k=v`, `--remote <addr>`) that would misread as
    session names. Daemons (`app-server`, `proxy`, `codex-threadripper`) and
    bare TUIs never match; `codex fork <sid>` deliberately does NOT match
    either — the fork process is minting a NEW session, so binding the
    PARENT sid to the fork's pid would make the parent read alive with the
    child's pid (a wrong takeover target).
    """
    if not argv or os.path.basename(argv[0]) != BASENAME:
        return None
    rest = argv[1:]
    if "resume" not in rest:
        return None
    at = rest.index("resume")
    if "exec" in rest[:at]:
        return None
    after = rest[at + 1 :]
    if not after or after[0].startswith("-"):
        return None
    return after[0]


def is_tui_process(record: ProcCli) -> bool:
    """PURE: does this codex process look like a session-holding interactive
    TUI (bare `codex [PROMPT]`, `codex resume …`, `codex fork …`)? Identity
    stays argv0-basename only (codex does not rewrite its title — C1
    hardened only kimi's identity set). Feeds the unbound-live hint and
    metadata-binding candidacy — never liveness by itself. Token-presence
    matching mirrors clap's own subcommand resolution: a flag value or
    quoted prompt spelling a denylisted subcommand only costs a missed hint
    (safe direction), never a daemon entering either source.
    """
    argv = record.argv
    if not argv or os.path.basename(argv[0]) != BASENAME:
        return False
    return not any(tok in _NON_TUI_SUBCOMMANDS for tok in argv[1:])


def sid_extractor(name_to_sid: Mapping[str, str]) -> ArgvExtractor:
    """THE codex argv→sid binding rule. Both consumers — the generation scan
    (`scan_non_claude`) and the execution-time takeover resolver
    (`resolve_argv_execution`) — reach it via `discover`, so argv evidence
    turns into a sid in exactly one place.

    A UUID target binds directly ("UUIDs take precedence if it parses" —
    upstream help); any other target is a thread name, bound only through
    the unique-name view of the index (`_name_index`). Unknown and ambiguous
    names bind nothing: a wrong guess would aim `s`/takeover SIGTERM at the
    wrong process, so the blind spot is kept instead (fail closed).
    """

    def extract(argv: tuple[str, ...]) -> str | None:
        target = extract_resume_target(argv)
        if target is None:
            return None
        if _UUID_RE.match(target):
            return target.lower()
        return name_to_sid.get(target)

    return extract


def _issue(source: str, path: str, detail: str) -> InventoryIssue:
    return InventoryIssue(source, path, detail)


def _read_index(
    home: Path, source: str, issues: list[InventoryIssue]
) -> dict[str, str]:
    """`session_index.jsonl` id → thread_name (last write wins)."""
    path = home / "session_index.jsonl"
    names: dict[str, str] = {}
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"id"' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue  # torn tail line of an append-only index
                sid = entry.get("id")
                name = entry.get("thread_name")
                if isinstance(sid, str) and isinstance(name, str) and name:
                    names[sid.lower()] = name
    except FileNotFoundError:
        pass  # older codex without the index — labels degrade, no issue
    except OSError as exc:
        issues.append(_issue(source, os.fspath(path), str(exc)))
    return names


def _name_index(names: Mapping[str, str]) -> dict[str, str]:
    """PURE: reverse the id→thread_name index into thread_name→sid, keeping
    ONLY names owned by exactly one sid — a name shared by several sids is
    ambiguous and binds nothing (fail closed). Built from the last-write-wins
    id→name view, so after a rename the OLD name is stale evidence and stops
    binding too."""
    owners: dict[str, str] = {}
    ambiguous: set[str] = set()
    for sid, name in names.items():
        if owners.setdefault(name, sid) != sid:
            ambiguous.add(name)
    for name in ambiguous:
        del owners[name]
    return owners


class CodexProvider:
    """One codex identity = one state home (ADR-0008).

    A machine with a single codex identity instantiates this once with
    `home=None`, which keeps following `cfg.codex_home` at call time — so
    the pre-multi-home behavior (and every test that monkeypatches
    `cfg.codex_home`) is unchanged. Operator-declared instances each pin
    their own home and carry `CODEX_HOME` in `env`, so every command csctl
    synthesizes states its identity explicitly instead of inheriting
    whatever environment csctl happens to run in.
    """

    basename = BASENAME
    capture_basenames = frozenset({BASENAME})  # no title rewrite observed
    #: Whitelisted `/proc/<pid>/environ` key: which home a running codex uses.
    #: Read only for processes the argv walk already matched, and only this
    #: key is retained — an environ block otherwise carries secrets.
    env_keys = frozenset({"CODEX_HOME"})
    caps = ProviderCaps(
        fork=True,  # native `codex fork <sid>`
        takeover=True,  # argv-exact + dispatch-metadata matches only
        liveness=LivenessGrade.TMUX,
    )

    def __init__(
        self,
        key: str = "codex",
        label: str = "cx",
        home: Path | None = None,
    ) -> None:
        self.key = key
        self.label = label
        #: None = follow `cfg.codex_home` (single-instance mode, resolved per
        #: call so monkeypatched config still flows through).
        self._home = home

    @property
    def home(self) -> Path:
        return self._home if self._home is not None else cfg.codex_home

    @property
    def declared(self) -> bool:
        """Whether an operator declaration pinned this instance's home."""
        return self._home is not None

    @property
    def env(self) -> Mapping[str, str]:
        """Environment every command for THIS identity must carry.

        Empty in single-instance mode: csctl then adds nothing to commands
        that already behave correctly, keeping copied commands clean. A
        declared instance always states `CODEX_HOME`, because the shell that
        runs the command may well have inherited a different identity's.
        """
        if not self.declared:
            return {}
        return {"CODEX_HOME": os.fspath(self.home)}

    @property
    def window_tag(self) -> str:
        """Launcher window leaf: the key with `:` swapped out, since a colon
        is tmux target syntax. Single-instance stays plain `codex`, so
        existing window names are untouched."""
        return self.key.replace(":", "-")

    @property
    def _source(self) -> str:
        """Issue-source tag; keyed so one identity's degraded store is
        distinguishable from another's in the status line."""
        return f"{self.key} sessions"

    def available(self) -> bool:
        # Home existence, per ADR-0005 — a fresh install with zero sessions
        # must still activate (launcher `x`); discover() tolerates the
        # missing sessions tree.
        return self.home.is_dir()

    def trusted_dirs(self) -> TrustScan:
        return read_trusted_dirs(self.home)

    def resume_argv(self, sid: str, fork: bool = False) -> list[str]:
        return ["codex", "fork" if fork else "resume", sid]

    def new_session_argv(self) -> list[str]:
        return ["codex"]

    def unarchive_argv(self, sid: str) -> list[str]:
        """Official recovery for an archived rollout (`codex unarchive
        <SESSION>`, verified against `codex --help`) — the honest hand-back
        when the resume family refuses an archived row."""
        return ["codex", "unarchive", sid]

    def delete_argv(self, sid: str) -> list[str]:
        """The official by-id deletion ("Permanently delete a saved session
        by id or session name" — `codex delete --help`, 0.146.0)."""
        return ["codex", "delete", sid]

    def delete_session_result(self, sid: str) -> CliDeleteResult:
        """Bounded official delete (`DeleteVerbs`): list argv only, never a
        shell — the sid rides `subprocess.run`'s argv boundary. Callers gate
        on fresh evidence first (`providers.execute_cli_delete`); this seam
        only runs the CLI and keeps typed stage/exit/stderr-tail evidence."""
        argv = self.delete_argv(sid)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_DELETE_TIMEOUT_SECONDS,
                # A declared instance deletes from ITS OWN store: without
                # this the CLI would resolve the sid against whatever home
                # csctl's own environment points at (ADR-0008).
                env={**os.environ, **self.env} if self.env else None,
            )
        except subprocess.TimeoutExpired:
            return CliDeleteResult(
                CliDeleteState.FAILED,
                CliDeleteStage.INVOKE,
                f"codex delete timed out after {_DELETE_TIMEOUT_SECONDS} seconds",
            )
        except FileNotFoundError:
            return CliDeleteResult(
                CliDeleteState.FAILED,
                CliDeleteStage.INVOKE,
                "codex executable not found on PATH",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            return CliDeleteResult(CliDeleteState.FAILED, CliDeleteStage.INVOKE, detail)
        if completed.returncode != 0:
            tail = _output_tail(completed)
            detail = f"codex delete exited with status {completed.returncode}"
            return CliDeleteResult(
                CliDeleteState.FAILED,
                CliDeleteStage.CLI,
                f"{detail}: {tail}" if tail else detail,
                returncode=completed.returncode,
            )
        return CliDeleteResult(
            CliDeleteState.DELETED,
            CliDeleteStage.CLI,
            returncode=completed.returncode,
        )

    def window_name(self, sid: str, fork: bool = False) -> str:
        base = f"{self.label}-{sid[:8]}"
        return f"{base}-fork" if fork else base

    def owns_process(self, record: ProcCli) -> bool:
        """PURE: could this bare codex process belong to THIS identity?

        Both identities run the same binary with identical argv (a launcher
        that `exec`s codex leaves no trace in argv0), so the only evidence is
        the process's own `CODEX_HOME`; absent that key it uses codex's
        default home. An environ that could not be read (`env is None`)
        proves nothing, and every identity keeps the process as a candidate:
        this feeds ONLY the unbound-live hint, where a redundant "possibly
        held" marker costs one extra confirmation while a missing one loses
        a double-open warning — so no evidence must fail toward warning.

        Deliberately NOT applied to liveness: argv/metadata bindings are the
        kill targets, and they are already identity-safe (a sid resolves
        only against the home whose rollout tree records it, and dispatch
        metadata carries the instance key). Filtering them on environ would
        instead DROP real bindings whenever `/proc`环境 is unreadable.
        """
        if record.env is None:
            return True
        declared = record.env.get("CODEX_HOME")
        home = (
            Path(os.path.normpath(Path(declared).expanduser()))
            if declared
            else codex_default_home()
        )
        return home == Path(os.path.normpath(self.home))

    def discover(
        self,
        cli_inventory: ProcCliInventory,
        cur: AbstractSet[int],
        panes: PaneInventory | None = None,
    ) -> ProviderScan:
        issues: list[InventoryIssue] = []
        names = _read_index(self.home, self._source, issues)
        extract = sid_extractor(_name_index(names))
        live = build_live_index(
            # Liveness consumes EVERY codex process: bindings are already
            # identity-safe, and dropping records on environ evidence would
            # lose real kill targets when it is unreadable (see owns_process).
            cli_inventory.records,
            extract,
            cur,
            panes=panes,
            provider_key=self.key,
            is_tui_process=is_tui_process,
            ancestors_of=proc.probe_ancestors,
        )

        active_root = self.home / "sessions"
        archived_root = self.home / "archived_sessions"
        if not active_root.is_dir() and not archived_root.is_dir():
            return ProviderScan()  # fresh install — nothing recorded yet
        active = self._scan_root(active_root, names, live, issues, archived=False)
        archived = self._scan_root(archived_root, names, live, issues, archived=True)
        # A sid on both sides is defensive-only (upstream archive is a MOVE):
        # the archived copy is stale evidence and the active row wins
        # regardless of mtime — the dict union's right operand does that.
        best = archived | active
        rows = apply_unbound_hints(
            best.values(),
            unbound_live_cwds(
                # Hints DO narrow to this identity: another home's bare TUI
                # must not mark this home's rows as possibly held.
                tuple(r for r in cli_inventory.records if self.owns_process(r)),
                extract,
                is_tui_process,
                bound_pids(live),
            ),
        )
        return ProviderScan(rows, tuple(issues))

    def _scan_root(
        self,
        root: Path,
        names: dict[str, str],
        live: dict[str, ArgvMatch],
        issues: list[InventoryIssue],
        *,
        archived: bool,
    ) -> dict[str, Session]:
        """One rollout tree → newest-rollout-per-sid rows.

        Shared by the active date tree and the flat `archived_sessions/`
        store — the first-line parse, read caps, label fallback, and source
        classification are exactly the ones the active tree uses, never a
        second copy. A missing root is normal and contributes no issue
        (`sessions/` appears on first run, `archived_sessions/` on first
        archive); an EXISTING but unreadable root degrades loudly via the
        walk-error issue without touching the other root's rows.
        """
        best: dict[str, Session] = {}
        if not root.is_dir():
            return best
        unparseable = 0
        over_cap = 0

        def _walk_error(exc: OSError) -> None:
            # os.walk swallows errors by default; an unreadable subtree must
            # surface as degradation, never silently narrow the list.
            issues.append(
                _issue(self._source, getattr(exc, "filename", "") or "", str(exc))
            )

        for dirpath, _dirnames, filenames in os.walk(root, onerror=_walk_error):
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    mtime = os.stat(path).st_mtime
                    payload, empty, truncated = self._row_payload(path)
                except OSError as exc:
                    issues.append(_issue(self._source, path, str(exc)))
                    continue
                if payload is None:
                    # An empty first line is a benign lazily-created rollout.
                    # A truncated one hit OUR read cap — an honest, distinct
                    # cause from a genuine parse failure. Anything else NON-
                    # empty may be upstream format drift. Both counted and
                    # surfaced once below, never conflated.
                    if truncated:
                        over_cap += 1
                    elif not empty:
                        unparseable += 1
                    continue
                row = self._project(
                    payload,
                    path,
                    mtime,
                    names,
                    live,
                    archived=archived,
                )
                if row is None:
                    continue
                kept = best.get(row.sid)
                if kept is None or row.mtime > kept.mtime:
                    best[row.sid] = row
        if over_cap:
            issues.append(
                _issue(
                    self._source,
                    os.fspath(root),
                    f"{over_cap} rollout file(s) whose first line exceeds "
                    f"the {FIRST_LINE_CAP}-byte read cap",
                )
            )
        if unparseable:
            issues.append(
                _issue(
                    self._source,
                    os.fspath(root),
                    f"{unparseable} rollout file(s) without a parseable "
                    "session_meta first line (upstream format change?)",
                )
            )
        return best

    def _row_payload(self, path: str) -> tuple[dict | None, bool, bool]:
        return read_meta(path)

    def _project(
        self,
        payload: dict,
        path: str,
        mtime: float,
        names: dict[str, str],
        live: dict[str, ArgvMatch],
        *,
        archived: bool,
    ) -> Session | None:
        if payload.get("thread_source") == "subagent":
            return None  # internal subagent rollout, not an operator work unit
        sid = payload.get("session_id") or payload.get("id")
        if not isinstance(sid, str) or not sid:
            return None
        sid = sid.lower()
        cwd = payload.get("cwd")
        source = classify_source(payload)
        match = live.get(sid)
        return Session(
            sid=sid,
            cwd=cwd if isinstance(cwd, str) else "",
            label=names.get(sid) or first_user_message(path) or "(untitled)",
            mtime=mtime,
            prompts=0,
            pid=match.pid if match else None,
            alive=match is not None,
            current=bool(match and match.current),
            provider=self.key,
            proc_start=match.proc_start if match else "",
            file=path,
            source=source,
            archived=archived,
        )
