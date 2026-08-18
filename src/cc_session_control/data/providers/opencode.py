"""opencode provider — disk discovery + argv-exact liveness (ADR-0005).

Upstream contracts (verified on opencode 1.18.15, this machine, 2026-08-18,
re-verify per release): state lives under the XDG data home
(`$XDG_DATA_HOME/opencode`, default `~/.local/share/opencode` — `opencode
debug paths`; there is no OPENCODE_HOME-style relocation variable).
Sessions are rows of the `session` table in `opencode.db` (SQLite, WAL):
`id` (`ses_…`), `directory` (the session's cwd), `title`, `parent_id`
(subagent sessions — skipped here like `opencode session list`'s roots),
and `time_updated` (epoch ms, projected to `Session.mtime`). Archived rows
(`time_archived NOT NULL`) are skipped: the CLI exposes no unarchive verb
(1.18.15), so csctl has no verified resume semantics for them — listing
them would either lie about resume or dangle a nonexistent recovery step
(the ADR-0005 capability discipline). Resume: `opencode --session <sid>`
(short `-s`); fork is a real verb (`--fork` with `--session`); delete:
`opencode session delete <sessionID>` (exit 1 + "Session not found" on a
missing sid, verified 2026-08-18). `--continue` carries no sid, so it binds
nothing.

A bare `opencode` TUI rewrites no process title (probed live 2026-08-18:
comm and argv0 both stay `opencode`), so argv evidence survives for the
process's whole life — bindings come from `--session` argv matches plus
csctl's own `@csctl_sid`/`@csctl_provider` window metadata for dispatched
windows. A bare TUI (or `--continue`) still leaves no pid↔session evidence
at all: opencode has no shell-hook seam (its plugin system is in-process
JS/TS, not a pid-reporting hook), so there is no runtime registry here like
kimi's — the unbound-live hint covers that gap.
"""

from __future__ import annotations

import os
import sqlite3
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
    apply_unbound_hints,
    bound_pids,
    build_live_index,
    flag_value,
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

BASENAME = "opencode"

# Non-interactive shapes from `opencode --help` (verified 1.18.15, this
# machine, 2026-08-18): servers/headless/utility subcommands never hold an
# interactive session, so they must not feed the unbound-live hint.
# `attach` is excluded too — it IS a TUI, but a client of a remote server:
# its cwd proves nothing about local sessions. The bare TUI (with an
# optional `[project]` positional), `--continue`, and `-s <sid>` DO hold
# sessions — they are exactly the shapes this predicate keeps.
_NON_TUI_SUBCOMMANDS = frozenset(
    {
        "completion",
        "acp",
        "mcp",
        "attach",
        "run",
        "debug",
        "providers",
        "auth",
        "agent",
        "upgrade",
        "uninstall",
        "serve",
        "web",
        "models",
        "stats",
        "export",
        "import",
        "github",
        "pr",
        "session",
        "plugin",
        "plug",
        "db",
        "help",
    }
)
_NON_TUI_FLAGS = frozenset({"-v", "--version", "-h", "--help"})

# Bounded `opencode session delete` invocation (same bounded-run +
# typed-outcome discipline as the codex twin); 10s matches the liveness
# seam's `claude agents --json` bound — a store operation that has not
# returned by then is a hang, not work.
_DELETE_TIMEOUT_SECONDS = 10
# Only the TAIL of a failing delete's output enters the typed detail: the
# last chars carry the actual error line, and notices must stay one line.
_DELETE_DETAIL_TAIL_CHARS = 200


def extract_sid(argv: tuple[str, ...]) -> str | None:
    """PURE: the sid an opencode process argv proves it is running."""
    if len(argv) < 2 or os.path.basename(argv[0]) != BASENAME:
        return None
    value = flag_value(argv[1:], "--session", "-s")
    if value and not value.startswith("-"):
        return value
    return None


def is_provider_process(record: ProcCli) -> bool:
    """PURE: opencode process identity. argv0 basename suffices — unlike
    kimi, this runtime rewrites no title (verified live 1.18.15,
    2026-08-18), so cmdline evidence is stable for the process's life."""
    return bool(record.argv) and os.path.basename(record.argv[0]) == BASENAME


def is_tui_process(record: ProcCli) -> bool:
    """PURE: session-holding interactive opencode TUI — process identity AND
    a non-daemon argv shape. Feeds the unbound-live hint and
    metadata-binding candidacy, never liveness by itself. Same conservative
    token matching as the codex/kimi twins: a false denylist hit (e.g. a
    project directory literally named `run`) only costs a missed
    hint/binding, never a server entering either source."""
    return is_provider_process(record) and not any(
        tok in _NON_TUI_SUBCOMMANDS or tok in _NON_TUI_FLAGS for tok in record.argv[1:]
    )


def _issue(path: str, detail: str) -> InventoryIssue:
    return InventoryIssue("opencode sessions", path, detail)


def _read_sessions(
    db_path: str,
) -> tuple[list[tuple[str, str, str, float]], InventoryIssue | None]:
    """Root, non-archived session rows as (sid, cwd, title, mtime), plus
    read-failure evidence.

    A MISSING database is not an issue (a fresh install has no state —
    callers get an empty list); an unreadable or schema-incompatible one
    surfaces as an issue (AGENTS.md 外部失败). Read-only URI open; WAL
    readers never block the CLI's writers."""
    if not os.path.exists(db_path):
        return [], None
    rows: list[tuple[str, str, str, float]] = []
    try:
        conn = sqlite3.connect(
            # as_uri() percent-encodes, so spaces/`?`/`#` in the path
            # cannot corrupt the mode=ro query parameter.
            f"{Path(db_path).as_uri()}?mode=ro",
            uri=True,
        )
        try:
            cursor = conn.execute(
                "SELECT id, directory, title, time_updated FROM session"
                " WHERE parent_id IS NULL AND time_archived IS NULL"
            )
            for sid, directory, title, time_updated in cursor:
                if not isinstance(sid, str) or not sid:
                    continue
                mtime = (
                    time_updated / 1000.0
                    if isinstance(time_updated, (int, float))
                    and not isinstance(time_updated, bool)
                    else 0.0
                )
                rows.append(
                    (
                        sid,
                        directory if isinstance(directory, str) else "",
                        title if isinstance(title, str) and title else "(untitled)",
                        mtime,
                    )
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], _issue(db_path, str(exc))
    return rows, None


def _output_tail(completed: subprocess.CompletedProcess[str]) -> str:
    text = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    if len(text) > _DELETE_DETAIL_TAIL_CHARS:
        return "…" + text[-_DELETE_DETAIL_TAIL_CHARS:]
    return text


class OpencodeProvider:
    key = "opencode"
    label = "oc"
    window_tag = "opencode"  # one instance — no disambiguation needed
    basename = BASENAME
    # No title rewrite (verified 1.18.15) — the plain basename nets every
    # opencode process.
    capture_basenames = frozenset({BASENAME})
    # csctl models ONE opencode state home (`cfg.opencode_home`), so no
    # environ evidence is needed to attribute a process (ADR-0008 covers
    # codex's multi-home case only).
    env_keys: frozenset[str] = frozenset()
    env: Mapping[str, str] = {}
    caps = ProviderCaps(
        fork=True,  # `--fork` with `--session` is a real verb (1.18.15)
        takeover=True,
        liveness=LivenessGrade.TMUX,  # argv-exact + dispatch-metadata matches
    )

    def available(self) -> bool:
        # Home existence, per ADR-0005 — a fresh install with zero sessions
        # must still activate (launcher `O`); discover() tolerates the
        # missing database.
        return cfg.opencode_home.is_dir()

    def resume_argv(self, sid: str, fork: bool = False) -> list[str]:
        argv = ["opencode", "--session", sid]
        if fork:
            argv.append("--fork")
        return argv

    def new_session_argv(self) -> list[str]:
        return ["opencode"]

    def delete_argv(self, sid: str) -> list[str]:
        """The official by-id deletion ("delete a session" — `opencode
        session delete --help`, 1.18.15)."""
        return ["opencode", "session", "delete", sid]

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
                f"opencode session delete timed out after {_DELETE_TIMEOUT_SECONDS} seconds",
            )
        except FileNotFoundError:
            return CliDeleteResult(
                CliDeleteState.FAILED,
                CliDeleteStage.INVOKE,
                "opencode executable not found on PATH",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            return CliDeleteResult(CliDeleteState.FAILED, CliDeleteStage.INVOKE, detail)
        if completed.returncode != 0:
            tail = _output_tail(completed)
            detail = (
                f"opencode session delete exited with status {completed.returncode}"
            )
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
        base = f"oc-{sid.removeprefix('ses_')[:8]}"
        return f"{base}-fork" if fork else base

    def discover(
        self,
        cli_inventory: ProcCliInventory,
        cur: AbstractSet[int],
        panes: PaneInventory | None = None,
    ) -> ProviderScan:
        live = build_live_index(
            cli_inventory.records,
            extract_sid,
            cur,
            panes=panes,
            provider_key=self.key,
            is_tui_process=is_tui_process,
            ancestors_of=proc.probe_ancestors,
        )
        db_path = os.fspath(cfg.opencode_home / "opencode.db")
        rows, db_issue = _read_sessions(db_path)
        if db_issue is not None:
            return ProviderScan(issues=(db_issue,))

        sessions = apply_unbound_hints(
            (
                self._project(sid, cwd, title, mtime, live)
                for sid, cwd, title, mtime in rows
            ),
            unbound_live_cwds(
                cli_inventory.records,
                extract_sid,
                is_tui_process,
                bound_pids(live),
            ),
        )
        return ProviderScan(sessions)

    def _project(
        self,
        sid: str,
        cwd: str,
        title: str,
        mtime: float,
        live: dict,
    ) -> Session:
        match = live.get(sid)
        return Session(
            sid=sid,
            cwd=cwd,
            label=title,
            mtime=mtime,
            prompts=0,
            pid=match.pid if match else None,
            alive=match is not None,
            current=bool(match and match.current),
            provider=self.key,
            proc_start=match.proc_start if match else "",
            # The conversation body lives in the same SQLite store
            # (`session_message`/`part` tables), not in any per-session
            # file — there is no transcript path for body search.
            file="",
            source="cli",
        )
