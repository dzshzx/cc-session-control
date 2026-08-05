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
see `_first_user_message`) looking for the first real `user_message` event
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

from ...config import cfg
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
)

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

# First-line pre-check (same cheap-substring-guard pattern as transcripts.py).
_META_MARK = b'"session_meta"'
# 256KB — real-machine session_meta lines have been observed at ~35KB, so the
# old 64KB cap left under 2x headroom; a line that reaches this cap without a
# trailing newline is truncated, not malformed (see `_read_meta`).
_FIRST_LINE_CAP = 256 * 1024

# Label-fallback continuation read past the session_meta line: bounded by
# BOTH line count and byte count so a huge rollout is never read in full —
# same cheap-substring-guard discipline as `_read_meta`/transcripts.py.
_USER_MSG_MARK = b'"user_message"'
_BODY_SCAN_MAX_LINES = 64
_BODY_SCAN_MAX_BYTES = 128 * 1024

# The only ChatGPT-mobile-remote originator observed on this machine (real
# rollout `session_meta`, 2026-08-04). Matched exactly, never as a prefix or
# substring guess: an unrecognized future originator must fall through to the
# honest "cli" default rather than a wrong badge.
_ANDROID_REMOTE_ORIGINATOR = "codex_chatgpt_android_remote"

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


def _issue(path: str, detail: str) -> InventoryIssue:
    return InventoryIssue("codex sessions", path, detail)


def _read_index(issues: list[InventoryIssue]) -> dict[str, str]:
    """`session_index.jsonl` id → thread_name (last write wins)."""
    path = cfg.codex_home / "session_index.jsonl"
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
        issues.append(_issue(os.fspath(path), str(exc)))
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


def _read_meta(path: str) -> tuple[dict | None, bool, bool]:
    """(parsed `session_meta` payload or None, first-line-was-empty,
    first-line-was-truncated-by-the-cap).

    A line that reads out to exactly `_FIRST_LINE_CAP` bytes without a
    trailing newline was cut off by the bounded read, not malformed by
    upstream — that's a distinct, honest cause from a genuine parse failure
    and must not be blamed on "upstream format change?" (see `discover`).
    """
    with open(path, "rb") as fh:
        raw = fh.readline(_FIRST_LINE_CAP)
    if not raw.strip():
        return None, True, False
    if len(raw) == _FIRST_LINE_CAP and not raw.endswith(b"\n"):
        return None, False, True
    if _META_MARK not in raw:
        return None, False, False
    try:
        record = json.loads(raw)
    except ValueError:
        return None, False, False
    payload = record.get("payload")
    return (
        (payload, False, False) if isinstance(payload, dict) else (None, False, False)
    )


def _classify_source(payload: dict) -> str:
    """PURE: coarse `Session.source` bucket from a codex session_meta payload.

    Priority (highest first, first match wins):
      1. `originator == "codex_exec"` — the pre-existing bridge/SDK signal
         (`Session.bridge_or_sdk` keys off `source == "sdk"`); a headless
         exec run must stay hidden-by-default no matter what else is set.
      2. session_meta `source == "vscode"` — reuses the IDE badge Claude
         rows already use for `entrypoint == "claude-vscode"`, so Desktop/
         IDE-launched codex sessions get an honest badge instead of "CLI".
      3. `originator == _ANDROID_REMOTE_ORIGINATOR` (exact) — ChatGPT mobile/
         remote launches, which are typically app-server-hosted and often
         show dead in `/proc`; the "CLI" badge would wrongly imply a direct
         terminal takeover is available.
      4. Otherwise "cli" — the honest default for a real interactive CLI
         session and for any originator/source this machine hasn't observed.
    """
    if payload.get("originator") == "codex_exec":
        return "sdk"
    if payload.get("source") == "vscode":
        return "vscode"
    if payload.get("originator") == _ANDROID_REMOTE_ORIGINATOR:
        return "remote"
    return "cli"


def _clean_label(text: str) -> str:
    """Collapse whitespace/newlines the same way transcripts.py cleans prompts."""
    return " ".join(text.split()).strip()


def _is_wrapper_block(text: str) -> bool:
    """A leading `<...>` block (`<user_instructions>`, `<environment_context>`,
    …) is injected context, not something the operator typed — skip it."""
    return text.startswith("<")


def _first_user_message(path: str) -> str | None:
    """Bounded continuation read past the session_meta first line: the first
    real `user_message` event body, cleaned — or None if nothing usable
    turns up within the line/byte caps. Malformed lines are skipped
    silently; this is a best-effort label fallback, not a completeness
    signal, so it never raises and never contributes an `InventoryIssue`.
    """
    try:
        with open(path, "rb") as fh:
            fh.readline(_FIRST_LINE_CAP)  # skip the already-parsed session_meta line
            total_bytes = 0
            for line_number, raw in enumerate(fh, start=1):
                total_bytes += len(raw)
                if (
                    line_number > _BODY_SCAN_MAX_LINES
                    or total_bytes > _BODY_SCAN_MAX_BYTES
                ):
                    return None
                if _USER_MSG_MARK not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue  # malformed body line — keep scanning
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                event_payload = record.get("payload")
                if not isinstance(event_payload, dict):
                    continue
                if event_payload.get("type") != "user_message":
                    continue
                message = event_payload.get("message")
                if not isinstance(message, str):
                    continue
                cleaned = _clean_label(message)
                if not cleaned or _is_wrapper_block(cleaned):
                    continue
                return cleaned
    except OSError:
        return None
    return None


class CodexProvider:
    key = "codex"
    label = "cx"
    basename = BASENAME
    capture_basenames = frozenset({BASENAME})  # no title rewrite observed
    caps = ProviderCaps(
        fork=True,  # native `codex fork <sid>`
        takeover=True,  # argv-exact + dispatch-metadata matches only
        liveness=LivenessGrade.TMUX,
    )

    def available(self) -> bool:
        # Home existence, per ADR-0005 — a fresh install with zero sessions
        # must still activate (launcher `x`); discover() tolerates the
        # missing sessions tree.
        return cfg.codex_home.is_dir()

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
        base = f"cx-{sid[:8]}"
        return f"{base}-fork" if fork else base

    def discover(
        self,
        cli_inventory: ProcCliInventory,
        cur: AbstractSet[int],
        panes: PaneInventory | None = None,
    ) -> ProviderScan:
        issues: list[InventoryIssue] = []
        names = _read_index(issues)
        extract = sid_extractor(_name_index(names))
        live = build_live_index(
            cli_inventory.records,
            extract,
            cur,
            panes=panes,
            provider_key=self.key,
            is_tui_process=is_tui_process,
            ancestors_of=proc.probe_ancestors,
        )

        active_root = cfg.codex_home / "sessions"
        archived_root = cfg.codex_home / "archived_sessions"
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
                cli_inventory.records,
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
            issues.append(_issue(getattr(exc, "filename", "") or "", str(exc)))

        for dirpath, _dirnames, filenames in os.walk(root, onerror=_walk_error):
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    mtime = os.stat(path).st_mtime
                    payload, empty, truncated = self._row_payload(path)
                except OSError as exc:
                    issues.append(_issue(path, str(exc)))
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
                    os.fspath(root),
                    f"{over_cap} rollout file(s) whose first line exceeds "
                    f"the {_FIRST_LINE_CAP}-byte read cap",
                )
            )
        if unparseable:
            issues.append(
                _issue(
                    os.fspath(root),
                    f"{unparseable} rollout file(s) without a parseable "
                    "session_meta first line (upstream format change?)",
                )
            )
        return best

    def _row_payload(self, path: str) -> tuple[dict | None, bool, bool]:
        return _read_meta(path)

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
        source = _classify_source(payload)
        match = live.get(sid)
        return Session(
            sid=sid,
            cwd=cwd if isinstance(cwd, str) else "",
            label=names.get(sid) or _first_user_message(path) or "(untitled)",
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
