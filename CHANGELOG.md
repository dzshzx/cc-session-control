# Changelog

## 0.4.0 (2026-06-30)

Major rework into a machine-wide operator panel for Claude Code's own
sessions, background agents, and Remote Control servers.

- **Three-tab TUI** — 会话 (Sessions), 后台 (Background agents), and 远程控制
  (Remote Control). Cleanup is now a submenu inside Sessions, not a separate tab.
- **Background agents tab** — view, respawn, resume/takeover, watch, stop, and
  remove background-agent jobs (`jobs/<short>/state.json`).
- **Liveness & identity** — `sessionId` is the primary key; a single liveness
  authority merges `claude agents --json`, the `sessions/*.json` registry, and a
  `/proc` starttime check that defeats pid reuse. Resume's multi-pid case is
  handled, and the session that launched `csctl` ("current") is protected from
  destructive ops.
- **Remote Control discovery** — RC servers are found via both tmux and a
  `/proc` walk; externally launched servers are surfaced read-only. Three
  independent bridge namespaces (`session_*` / `cse_*` / `env_*`) are modeled
  without conflation.
- **Bridge-environment ledger** — an append-only local ledger
  (`$XDG_CONFIG_HOME/csctl/environments.jsonl`) keeps toggled-away / orphaned
  cloud environments traceable, with an "orphan = ledger − file-referenced"
  manual-delete checklist (csctl cannot deregister cloud envs).
- **Two-strategy cleanup** — per-directory-key orphan sweep plus an age sweep,
  preview-first, excluding live + current sessions; wired into both the Sessions
  submenu and `csctl prune` (`--sweep-orphans` / `--sweep-zombies` / `--sweep-aged`).
- **Shared world snapshot** — one async scan per cycle feeds all three tabs;
  scanning never blocks the urwid loop and widgets are only mutated on the main loop.
- **Unified cross-tab keys** — shared verb vocabulary (`r`/`s`/`Enter`/`R`/`d`),
  confirm-on-kill modal across tabs, and honest cross-platform degradation when
  `/proc` is unavailable (destructive ops refuse rather than risk the wrong session).
- **Cross-platform safety** — liveness degrades gracefully on platforms without
  `/proc`; destructive operations refuse when "current" cannot be determined.
- PyPI publishing infrastructure: CI + release GitHub Actions workflows
  (Trusted Publishing on `v*` tags) and a maintainer release guide.

## 0.3.0 (2026-06-23)

- Relaunch a session into tmux under Remote Control (`R` key) so it outlives the
  terminal and is controllable from phone / claude.ai/code.
- Show hidden bridge/SDK sessions on demand (`h` toggle).
- Stop auto-restarting Remote Control servers (every restart minted a duplicate
  cloud environment); restart is now an explicit user action.
- Harden session filtering, cleanup, and tmux command handling.

## 0.2.0 (2026-06-23)

- Single-source the version via setuptools dynamic metadata.
- Unify resume kill semantics — a fork keeps the original session alive; a plain
  resume takes over.
- Harden the view contract (`TabView` Protocol) and self-invalidate the liveness cache.
- Single-source RC status, session artifact roots, and `~/.claude.json` reads.
- Rename RC toggles to clearer 开机自启 / 自动远控.
- Commit `uv.lock` for reproducible installs.

## 0.1.0 (2026-06-22)

Initial release.

- Sessions Tab: view, resume, terminate, delete Claude Code sessions
- Remote Control Tab: start/stop RC servers, toggle auto-start, crash recovery
- Cleanup Tab: prune empty/short sessions, sweep orphan directories
- CLI subcommands: `csctl rc`, `csctl prune`
- Cross-platform clipboard support (WSL/macOS/Wayland/X11)
- Auto-refresh every 10 seconds in TUI
