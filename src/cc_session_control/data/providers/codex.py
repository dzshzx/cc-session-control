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
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Set as AbstractSet

from ...config import cfg
from ..proc import ProcCliInventory
from ...models import InventoryIssue, Session
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


def extract_sid(argv: tuple[str, ...]) -> str | None:
    """PURE: the sid a codex process argv proves it is running.

    Matches only the resume/fork subcommand shapes with an explicit UUID —
    daemons (`app-server`, `proxy`, `codex-threadripper`) and bare TUIs never
    match, so they are never kill targets.
    """
    if len(argv) < 3 or os.path.basename(argv[0]) != BASENAME:
        return None
    rest = argv[1:]
    if "resume" not in rest and "fork" not in rest:
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


def _read_meta(path: str) -> dict | None:
    """The parsed `session_meta` payload of one rollout, or None."""
    with open(path, "rb") as fh:
        raw = fh.readline(_FIRST_LINE_CAP)
    if _META_MARK not in raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


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
        return (cfg.codex_home / "sessions").is_dir()

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
        best: dict[str, Session] = {}
        try:
            walker = os.walk(root)
        except OSError as exc:  # pragma: no cover - os.walk defers errors
            return ProviderScan(issues=(_issue(os.fspath(root), str(exc)),))
        for dirpath, _dirnames, filenames in walker:
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    mtime = os.stat(path).st_mtime
                    payload = self._row_payload(path)
                except OSError as exc:
                    issues.append(_issue(path, str(exc)))
                    continue
                if payload is None:
                    continue
                row = self._project(payload, path, mtime, names, live)
                if row is None:
                    continue
                kept = best.get(row.sid)
                if kept is None or row.mtime > kept.mtime:
                    best[row.sid] = row
        return ProviderScan(tuple(best.values()), tuple(issues))

    def _row_payload(self, path: str) -> dict | None:
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
            label=names.get(sid) or "(untitled)",
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
