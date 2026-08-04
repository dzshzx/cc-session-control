"""Codex CLI provider — disk discovery + argv-exact liveness (ADR-0005).

Upstream contracts (verified on Codex CLI 0.146.0, re-verify per release):
sessions are NDJSON rollouts at
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` whose FIRST line
is a `session_meta` record (`payload.id` == `payload.session_id` for
top-level rollouts; `thread_source: "subagent"` marks internal subagent
rollouts, skipped here). `session_index.jsonl` maps `id` → `thread_name`.
Resume/fork: `codex resume <sid>` / `codex fork <sid>`. Discovery reads
rollout first lines only — never whole files; zstd-compressed old rollouts
(`.jsonl.zst`) and `archived_sessions/` are out of scope.

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
from collections.abc import Set as AbstractSet

from ...config import cfg
from ...models import InventoryIssue, Session
from ..proc import ProcCliInventory
from .argv_live import build_argv_index
from .base import LivenessGrade, ProviderCaps, ProviderScan

BASENAME = "codex"

_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# First-line pre-check (same cheap-substring-guard pattern as transcripts.py).
_META_MARK = b'"session_meta"'
_FIRST_LINE_CAP = 64 * 1024

# Label-fallback continuation read past the session_meta line: bounded by
# BOTH line count and byte count so a huge rollout is never read in full —
# same cheap-substring-guard discipline as `_read_meta`/transcripts.py.
_USER_MSG_MARK = b'"user_message"'
_BODY_SCAN_MAX_LINES = 64
_BODY_SCAN_MAX_BYTES = 128 * 1024


def extract_sid(argv: tuple[str, ...]) -> str | None:
    """PURE: the sid a codex process argv proves it is running.

    Matches ONLY the resume subcommand shape with an explicit UUID — daemons
    (`app-server`, `proxy`, `codex-threadripper`) and bare TUIs never match,
    so they are never kill targets. `codex fork <sid>` deliberately does NOT
    match either: the fork process is minting a NEW session, so binding the
    PARENT sid to the fork's pid would make the parent read alive with the
    child's pid (a wrong takeover target).
    """
    if len(argv) < 3 or os.path.basename(argv[0]) != BASENAME:
        return None
    rest = argv[1:]
    if "resume" not in rest:
        return None
    for tok in rest:
        if _UUID_RE.match(tok):
            return tok.lower()
    return None


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


def _read_meta(path: str) -> tuple[dict | None, bool]:
    """(parsed `session_meta` payload or None, first-line-was-empty)."""
    with open(path, "rb") as fh:
        raw = fh.readline(_FIRST_LINE_CAP)
    if not raw.strip():
        return None, True
    if _META_MARK not in raw:
        return None, False
    try:
        record = json.loads(raw)
    except ValueError:
        return None, False
    payload = record.get("payload")
    return (payload, False) if isinstance(payload, dict) else (None, False)


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
                if line_number > _BODY_SCAN_MAX_LINES or total_bytes > _BODY_SCAN_MAX_BYTES:
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
    caps = ProviderCaps(
        fork=True,  # native `codex fork <sid>`
        takeover=True,  # argv-exact matches only
        liveness=LivenessGrade.ARGV,
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

    def window_name(self, sid: str, fork: bool = False) -> str:
        base = f"cx-{sid[:8]}"
        return f"{base}-fork" if fork else base

    def discover(
        self,
        cli_inventory: ProcCliInventory,
        cur: AbstractSet[int],
    ) -> ProviderScan:
        issues: list[InventoryIssue] = []
        names = _read_index(issues)
        live = build_argv_index(cli_inventory.records, extract_sid, cur)

        root = cfg.codex_home / "sessions"
        if not root.is_dir():
            return ProviderScan()  # fresh install — nothing recorded yet
        best: dict[str, Session] = {}
        unparseable = 0

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
                    payload, empty = self._row_payload(path)
                except OSError as exc:
                    issues.append(_issue(path, str(exc)))
                    continue
                if payload is None:
                    # An empty first line is a benign lazily-created rollout;
                    # a NON-empty unparseable one may be upstream format
                    # drift — counted and surfaced once below.
                    if not empty:
                        unparseable += 1
                    continue
                row = self._project(payload, path, mtime, names, live)
                if row is None:
                    continue
                kept = best.get(row.sid)
                if kept is None or row.mtime > kept.mtime:
                    best[row.sid] = row
        if unparseable:
            issues.append(
                _issue(
                    os.fspath(root),
                    f"{unparseable} rollout file(s) without a parseable "
                    "session_meta first line (upstream format change?)",
                )
            )
        return ProviderScan(tuple(best.values()), tuple(issues))

    def _row_payload(self, path: str) -> tuple[dict | None, bool]:
        return _read_meta(path)

    def _project(
        self,
        payload: dict,
        path: str,
        mtime: float,
        names: dict[str, str],
        live: dict,
    ) -> Session | None:
        if payload.get("thread_source") == "subagent":
            return None  # internal subagent rollout, not an operator work unit
        sid = payload.get("session_id") or payload.get("id")
        if not isinstance(sid, str) or not sid:
            return None
        sid = sid.lower()
        cwd = payload.get("cwd")
        originator = payload.get("originator")
        # `codex exec` headless runs map onto the existing bridge/SDK hide
        # filter (`Session.bridge_or_sdk` keys off source == "sdk").
        source = "sdk" if originator == "codex_exec" else "cli"
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
        )
