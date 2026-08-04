"""Kimi Code provider — disk discovery + argv-exact liveness (ADR-0005).

Upstream contracts (verified on Kimi Code 0.31.1, re-verify per release):
`$KIMI_CODE_HOME/session_index.jsonl` maps `sessionId` → `sessionDir` /
`workDir`; each session dir holds `state.json` (`title`, `lastPrompt`,
`workDir`, `updatedAt`). Resume: `kimi --session <sid>` (short `-S`); fork
exists only as the in-session `/fork`, so `caps.fork` is False and a fork
argv request is a programming error. A bare `kimi` REPL leaves no
pid↔session evidence (no fd, no env — probed), so only `--session` argv
matches bind; those are the only kill targets.
"""

from __future__ import annotations

import json
import os
from collections.abc import Set as AbstractSet

from ...config import cfg
from ..proc import ProcCliInventory
from ...models import InventoryIssue, Session
from .argv_live import build_argv_index, flag_value
from .base import LivenessGrade, ProviderCaps, ProviderScan

BASENAME = "kimi"


def extract_sid(argv: tuple[str, ...]) -> str | None:
    """PURE: the sid a kimi process argv proves it is running."""
    if len(argv) < 2 or os.path.basename(argv[0]) != BASENAME:
        return None
    value = flag_value(argv[1:], "--session", "-S")
    if value and not value.startswith("-"):
        return value
    return None


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


class KimiProvider:
    key = "kimi"
    label = "km"
    basename = BASENAME
    caps = ProviderCaps(
        takeover=True,  # argv-exact matches only
        liveness=LivenessGrade.ARGV,
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
    ) -> ProviderScan:
        issues: list[InventoryIssue] = []
        live = build_argv_index(cli_inventory.records, extract_sid, cur)
        index_path = cfg.kimi_home / "session_index.jsonl"
        entries: dict[str, dict] = {}
        try:
            with open(index_path, "rb") as fh:
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
            return ProviderScan()  # no kimi state — nothing to list
        except OSError as exc:
            return ProviderScan(issues=(_issue(os.fspath(index_path), str(exc)),))

        rows = tuple(
            self._project(sid, entry, live, issues)
            for sid, entry in entries.items()
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
        cwd = entry.get("workDir") if isinstance(entry.get("workDir"), str) else ""
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
            file=os.path.join(session_dir, "state.json")
            if isinstance(session_dir, str)
            else "",
            source="cli",
        )
