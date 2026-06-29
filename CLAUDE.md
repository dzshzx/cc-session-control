# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cc-session-control` (CLI: `csctl`) is a machine-wide operator panel for **Claude Code's own** sessions, background agents, and Remote Control servers. It reads Claude Code's on-disk state (`~/.claude/projects/*/*.jsonl` transcripts, `~/.claude/sessions/*.json` + `~/.claude/jobs/*/state.json` registries, `~/.claude.json`), walks `/proc`, and shells out to the `claude` CLI and `tmux` — it is an operator tool *for* Claude Code, not a general app. The TUI has three tabs: **会话 (Sessions)**, **后台 (Background agents)**, and **远程控制 (Remote Control)**; cleanup is a submenu inside Sessions, not a tab.

## Commands

```bash
# Install / upgrade csctl FOR USE — ALWAYS from the public GitHub repo, never a
# local editable/direct install (keeps the tool you run decoupled from your checkout).
# Requires Python 3.12+.
uv tool install git+https://github.com/dzshzx/cc-session-control.git
#   On this machine csctl is a uv tool at ~/.local/bin/csctl — it is NOT mise-managed
#   (there is no mise pin for it; `mise install`/`mise uninstall …pipx…` are no-ops here).
#   After pushing, refresh the installed build from the latest GitHub HEAD with a FORCED
#   rebuild (plain `uv tool upgrade` may keep the cached git ref and skip the new HEAD):
#     uv tool install --reinstall git+https://github.com/dzshzx/cc-session-control.git
#   Verify the running build: csctl --version

# Run the installed TUI (the GitHub build)
csctl

# Dev/test ONLY — uv manages a transient .venv here; this is NOT how csctl is installed
# for use. Do not treat the editable .venv as the csctl you run day-to-day.
uv run --extra dev pytest tests/                                                # all
uv run --extra dev pytest tests/test_views.py::test_sessions_view_filter_logic  # single test
uv run csctl                                                                    # exercise local source changes

# Guardrail enforced for contributions (must return nothing)
grep -rn --include='*.py' '/home/' src/      # no hardcoded paths in product source
```

Constraints from `CONTRIBUTING.md`: keep each source file **under 600 lines**, use type hints, no hardcoded paths.

## Architecture

The UI toolkit is **urwid** (the only runtime dependency is `urwid>=2.0.0`). Three layers under `src/cc_session_control/`:

- **`data/`** — everything that touches external state, both reads *and* writes. It has an internal **bottom→top DAG** (no cycles):
  - bottom (pure IO + parse): `proc.py` (the ONLY `/proc` seam — `proc_starttime`/`pid_alive`/`ancestor_pids`/`scan_rc_servers`), `registry.py` (`sessions/*.json` + `jobs/*/state.json`, ~5s TTL cache).
  - middle: `liveness.py` (the ONE liveness authority — `alive_map`/`invalidate_cache`/pure `live_index`), `environments.py` (the bridge-environment ledger — **must never import `rc`**), `cleanup.py` (per-dir-key + age cleanup strategies).
  - top (assemble): `sessions.py` (parse transcripts → `Session`), `rc.py` (tmux + trust + `/proc` server discovery → `RCProject`/`RCServer`; calls `environments.upsert` one-way).
  - `agents.py` is a **zero-logic re-export shim** for `liveness.alive_map`/`invalidate_cache` (kept so old imports + the `session_ops.invalidate_cache` monkeypatch keep working).
  - `snapshot.py` sits ABOVE the rest (composes them into one `WorldSnapshot`); nothing in `data/` imports it (only `app`/`views` do).
  Returns the dataclasses in `models.py` (`Session`, `SessionProc`, `AgentJob`, `LiveInfo`, `RCProject`, `RCServer`, `EnvRecord`, `BridgeEnv`).
- **`actions/`** — operations that don't belong in `data/`: `session_ops.py` (`terminate_session`, `resume_cmd`/`do_resume`, `relaunch_in_tmux`, `to_clipboard`) and `agent_ops.py` (background-agent lifecycle: `respawn`/`remove_job`/`watch`/`resume_takeover`/`stop_job`, with `job_host` joining sid→`sessions/<pid>.json`).
- **`views/`** — urwid widgets per tab (`sessions.py` + `_session_row.py`, `agents.py`, `rc.py`). `app.py` orchestrates them; `cli.py` is the argparse entry point; `config.py` holds the global `cfg` singleton **and is the single path authority** (`cfg.sessions_dir`/`jobs_dir`/`environments_ledger`/the cleanup dirs/`cleanup_age_days`) — never inline `claude_home / "..."` elsewhere.

The invariant is **import direction, not purity**: `views` import from `data`/`actions`; `data`/`actions` never import upward; within `data` the DAG above is one-way (notably `environments` never imports `rc`). There is no separate "pure read vs side effect" split — `data/` holds both.

### The view contract (how `app.py` drives tabs generically)

`App` holds `self.views: list[TabView]` and drives each via the `TabView` `Protocol` (defined in `app.py`, `@runtime_checkable`). To add/modify a tab, satisfy that Protocol structurally — these members:

- `.widget` — the urwid widget for the tab body
- `._loaded` — bool; whether `load()` has run
- `load()` — synchronous initial scan + first render (called once in `run()` for the startup tab; switching to an as-yet-unloaded tab triggers an async refresh instead)
- `fetch_pending(snapshot=None)` — **runs on the worker thread**; projects the shared `WorldSnapshot` into `self._pending` (never touches widgets). The `snapshot` arg is **optional**: called with `None` (build failed, or a unit test) the view self-fetches its own slice — so every view stays back-compatible and testable in isolation.
- `apply_data()` — **runs on the main loop**; swaps `_pending` into the live walker
- `keyhints() -> str` — footer hint string for the current mode
- `handle_key(key)` — handles every key except `Tab` and `q`

`App._input` handles only `tab` (switch) and `q` (quit); everything else is forwarded to the active view's `handle_key`. Adding a tab means updating `self.views` **and** `TAB_NAMES` **and** the `_switch_tab` cycle together (they index in lockstep).

### Async refresh + shared world snapshot (the threading model)

Scanning hits the filesystem, `/proc`, and subprocesses, so it must not block the urwid loop, and the three tabs must not each re-scan it (R11/D8). The pattern (`App.trigger_async_refresh`):

1. A daemon thread computes **one** `data/snapshot.py::build_world_snapshot()` for the whole cycle (transcripts, registries, `/proc` walk, RC servers — each scanned ONCE), then calls `view.fetch_pending(snapshot)` on each view. A failed build degrades to `fetch_pending(None)` (per-view self-fetch). Views write only their `_pending` field and **never touch widgets directly**.
2. The thread writes one byte to a pipe registered via `loop.watch_pipe(self._on_pipe)`.
3. `_on_pipe` runs on the main loop and calls `apply_data()` on each view, which swaps `_pending` into the live walker.

A `self._refreshing` guard prevents overlapping refreshes. Auto-refresh re-arms every 10s via `set_alarm_in`. **Never mutate urwid widgets from the worker thread** — only set `_pending`. (`build_world_snapshot` also persists the bridge-environment ledger on the worker thread — that is IO on shared data, which is fine; the widget rule is the hard line.)

### Resume happens *outside* the UI loop (process replacement)

The TUI cannot run `claude` inside itself. To resume a session, `SessionsView` calls `app.exit_with_resume(session, fork)`, which exits the MainLoop returning a `("resume", session, fork)` tuple. Back in `cli._cmd_tui`, `do_resume` (in `actions/session_ops.py`) then `os.chdir`s to the session's cwd and `os.execvp("claude", ...)` — **replacing the csctl process**. This is why the final resume step lives in `cli.py`, not the view.

**Unified kill semantics:** `resume_cmd` (the `y`-key clipboard string) and `do_resume` (the actual exec) share one decision computed once in `_resume_plan`, which returns `should_kill = alive and not current and not fork`. A plain **resume takes over** a live session (kills the old pid first); a **fork is a copy** and leaves the original running. Both consumers must read `should_kill` rather than re-deriving the condition — that re-derivation was the old divergence (now removed).

**Relaunch into tmux (`R` key):** `relaunch_in_tmux` (in `actions/session_ops.py`) runs `claude --resume <sid> --remote-control <name>` in a tmux window (session `cfg.tmux_session`, default `cc`, created via `rc.run_in_tmux` — kept separate from the `rc` server windows) so the session **outlives the terminal** and is controllable from phone / claude.ai/code. Unlike `do_resume` it does **not** replace the csctl process — it just spawns the window, reusing the same `should_kill` handoff (live non-current session → old pid killed first; current session refused). Gotcha learned from a spike: killing the tmux window does **not** reliably kill a `--remote-control` process (it survives orphaning), so stop a relaunched session via the `s` terminate-by-pid action, not by closing its window.

**Unified cross-tab key table.** All three tabs share one verb vocabulary so muscle memory transfers: `r` = refresh (every tab), `s` = stop/kill a live thing (Sessions terminate — with a y/n confirm — / Agents stop / RC stop-one), `Enter` = the tab's primary action (Sessions 接回 / Agents 接管, also `o` / RC 启动), `R` = relaunch-into-tmux on Sessions and respawn on Agents, `d` = delete a settled record. Destructive single-key ops that confirm: Sessions `s` (terminate live) and RC `S` (stop-all), both via the App-level `App.confirm(message, on_yes)` modal in `app.py` (routed in `_input`; `_confirm_yes` gates it). `c` stays tab-specific (Sessions cleanup / RC toggle 自动远控) — it is not part of the universal verb set. RC `ServerRow`/`EnvRow` are `selectable() == False` (display-only; focus skips them).

### Liveness & identity — sessionId is the primary key

The **primary key is `sessionId`**, never pid. Liveness is a **multi-source merge** with `data/liveness.py` as the **one authority** (NOT `agents.py`, which is just a re-export shim):

- `data/liveness.py::alive_map()` runs `claude agents --json` (cached 5s) → `{sessionId: pid}` (agent sessions only — it does NOT list RC servers or reflect RC exposure).
- `data/registry.py::read_session_procs()` reads `sessions/<pid>.json` for richer per-runtime state (`status`/`procStart`/`kind`/`entrypoint`/`bridgeSessionId`). **A file existing ≠ alive** (most are zombies).
- `data/proc.py::pid_alive(pid, procStart)` confirms a pid is real: `/proc/<pid>` exists **and** its stat starttime (the 22nd field — parsed AFTER the last `)` because `comm` can contain spaces/parens) equals the recorded `procStart` (this defeats pid reuse).
- `data/liveness.py::live_index(session_procs, agents_map)` is a **pure, dependency-injected** function that merges the two sources by sid, handles **resume's multi-pid** (one sid → many `sessions/<pid>.json`; pick the proc-alive pid, keep ALL alive pids in `pids`), and returns `{sid: LiveInfo}`. `scan()` fetches the data, then calls it (so the merge is unit-testable without IO).

A sid is **alive** iff `pid_alive` holds for one of its pids OR it appears in `alive_map()`. Terminating is the only session op that changes liveness, and `terminate_session` / `stop_job` **invalidate the cache themselves** — callers don't call `invalidate_cache()` manually. (delete/cleanup only act on already-dead sessions.)

**"current" = self-protection.** `data/proc.py::ancestor_pids()` walks `/proc/<pid>/stat` up the parent chain to csctl's own ancestors. A session is **current** if ANY of its alive pids is in that set (multi-pid aware) — the session that launched csctl, protected: you cannot resume, terminate, or prune it.

**Cross-platform safety (R10).** `/proc` is **Linux/WSL only**. With no `/proc`, `pid_alive`/`ancestor_pids` return empty and liveness degrades to `alive_map()` — and because **"current" then cannot be determined**, destructive ops (terminate/delete/clean/stop/remove) **refuse** (`proc.current_determinable()` guards them) rather than risk hitting csctl's own session; the UI flags the degrade.

### Session model & transcript parsing

`sessions.scan()` globs `~/.claude/projects/*/*.jsonl` and line-scans each (a cheap substring pre-check guards every `json.loads` for speed — keep this pattern), then enriches each `Session` from `live_index()` + the registry: `kind`/`entrypoint`/`source` (cli/vscode/sdk/bg), `rc_exposed`, `env_id`, `agent_short`, `status`. Display `label` priority: `aiTitle` → first non-noise user prompt → `lastPrompt` → `(untitled)`. `_NOISE` / `_clean_text` strip command/system-reminder wrapper tags so prompts read cleanly. The 桥接/SDK hide filter keys off `Session.bridge_or_sdk` (D9: union of the transcript `hidden` tags and registry `source == "sdk"`) so the badge and the `h` toggle never disagree.

**RC exposure is a pure predicate.** `sessions._is_rc_exposed(bridge, pid_alive) = bool(bridge) and pid_alive` — a session's *session remote control* is "exposed" only when its `bridgeSessionId` is truthy AND its proc is alive (a zombie's stale bridge does NOT count). Unit-tested across the full missing/null/string × alive/dead matrix.

### Background agents (后台 tab)

The persistent truth for a background agent is `jobs/<short>/state.json` (`registry.read_agent_jobs` → `AgentJob`), NOT `sessions/`. `state.json` carries **no pid**, so `actions/agent_ops.py::job_host` JOINs `job.sid → sessions/<pid>.json` to find a stoppable host pid (a live worker with no sessions file is unstoppable — a documented orphan risk). Lifecycle ops: `respawn` (`claude --resume <resume_sid> <flags> --bg` via `shlex.join`, spawned in tmux — never replaces csctl), `resume_takeover` (adapts the job into a `Session` for the existing resume path), `watch` (read-only `timeline.jsonl`), `remove_job` (settled-only), `stop_job` (live-only, signals the joined host pid; killing a `--remote-control`/bg worker may not fully reap it — orphan risk surfaced in the UI).

### Remote Control: tmux servers, /proc discovery, three namespaces

**Managed RC servers are tmux windows** in a session named `rc` (env `CSCTL_RC_SESSION`). `rc.start_one` launches one `claude remote-control --name ws/<proj> --spawn same-dir` process. It deliberately does **not** auto-restart: every fresh Remote Control process registers a new cloud environment, and automatic restarts pile up duplicate mobile/web environment entries with the same display name. Status comes from tmux `#{pane_dead}`: `running` / `dead` / `stopped`; restart is an explicit user action.

All tmux access goes through a single seam: only `_tmux_run` touches `subprocess`; every other tmux call is a thin verb wrapper (`_tmux_new_window`, `_tmux_kill_window`, …) that keeps the swallow-errors contract. Add new tmux operations as wrappers, not raw `subprocess` calls.

**Discovery beyond tmux (`rc.scan_servers()`).** RC servers are also found by walking `/proc` (`proc.scan_rc_servers` + the pure `proc._match_rc_cmdline`: argv0 basename `claude` AND a `remote-control` subcommand token AND `--name` — codex, which uses a `--remote-control` *flag*, is excluded). A discovered pid that belongs to a csctl-managed tmux pane is **managed**; otherwise it's **external** and **read-only** (csctl never takes over / restarts it). Managed servers' `env_*` cloud id is grepped from the pane and pushed one-way into the ledger.

**Three Remote Control namespaces — independent, never linked by suffix:**
- **session remote control** → `bridgeSessionId: session_*` in `sessions/<pid>.json` (a foreground session exposing itself). Tri-state: key missing (never on) / `null` (transient — re-opening overwrites it) / string (exposed). "Exposed now" = the `_is_rc_exposed` predicate above.
- **background agent env** → `bridgeSessionId: cse_*` in `jobs/<short>/state.json`. A `cse_*` resume pair shares one suffix (same env); `session_*` and `cse_*` suffixes never coincide → only ever deduped *within* a namespace.
- **project rc server env** → `env_*`, printed only to the server's stdout/QR, with **zero** state file — the only local signal is the running process + its pane output.

**Bridge-environment ledger (`data/environments.py`, R6).** Claude Code keeps only the *current* binding for each session/agent (one overwritten field), so toggled-away or historically-minted cloud environments vanish from disk. csctl keeps its own append-only ledger (`$XDG_CONFIG_HOME/csctl/environments.jsonl`) so they stay traceable. It is a **passive store**: callers push observations in via `upsert(records)` — it **never imports `rc`** (the DAG is one-way `rc → environments`). Two observation tiers:
- `observe()` — bridge-truthy **FILE-REFERENCED** set (every env any on-disk file references right now, alive or zombie, + `env_*` passed in from rc servers). This defines ledger **membership**.
- `observe_live()` — **alive-gated** CURRENT/bound set (used for display; a zombie's stale bridge is NOT shown as bound).

`build_world_snapshot` (and `csctl env`) `upsert(observe(...))` **every cycle**, so an env that later toggles away stays in the ledger but drops out of the file-referenced set. Three coherent tiers: `active(alive) ⊆ file-referenced(in ledger now) ⊆ ledger(history)`, and **`orphan = ledger − file-referenced`** — those orphans are the manual-delete candidates. The ledger write is **write-on-change + `tmp+rename` atomic + `fcntl` advisory lock + retention/compaction**. **Capability red line:** csctl has **no deregister** — it can only forget locally and print a checklist; orphan lists are inherently incomplete (envs minted while csctl wasn't running can't be back-filled). The RC view labels orphans "云端需手动删除".

**Two independent "start" concepts — do not conflate them** (columns in the RC tab):
- `auto_start` ("开机自启") — project is in csctl's own list at `$XDG_CONFIG_HOME/csctl/rc-enabled`; controls what `csctl rc up` / the `A` key starts.
- `rc_at_startup` ("自动远控") — the per-project `remoteControlAtStartup` flag in `<proj>/.claude/settings.local.json`; controls whether **`claude` itself** enables Remote Control on launch. Tri-state (`True`/`False`/`None`=unset). `remoteControlSpawnMode` (also tri-state-ish, `None`=unset) rides alongside on `RCProject.spawn_mode`.

A project must be **trusted** (`hasTrustDialogAccepted` in `~/.claude.json`) before RC can start.

### Cleanup — two strategies, preview-first (`data/cleanup.py`, R7)

Cleanup logic lives entirely in `data/cleanup.py`; both the Sessions submenu and the `csctl prune` CLI drive it. Keys are **typed by directory, never assumed uuid==sid**:
- **Strategy A — per-dir key semantics.** `session-env`/`file-history`/`tasks`/`uploads` are **sid-keyed** → orphan = sid not in the known-session set. `sessions/` is **pid-keyed** → sweep zombies (`pid_alive` false), but **keep the current-bound pid and any current session's pid file**, and when one sid has multiple pids keep the alive ones. `debug/` is **debug-run-id-keyed** (NOT sid) → its own semantics.
- **Strategy B — age sweep.** `shell-snapshots`/`telemetry`/`plans`/`backups`/`paste-cache` are time/global-keyed → drop by mtime past `cfg.cleanup_age_days`.

Surface map: the Sessions submenu exposes empty/short session prune + sid-keyed orphan dirs; `csctl prune` exposes the same (`--sweep-orphans`) **plus** the pid-keyed zombie sweep (`--sweep-zombies`) and the age sweep (`--sweep-aged`). All cleanup is **preview-first** (CLI dry-run unless `--apply`), **excludes live + current**, and **refuses when current can't be determined** (no `/proc`, R10) — except the age sweep, which is mtime-only and session-agnostic. `jobs/` is never swept automatically (only the explicit `agent_ops.remove_job` on a settled agent removes a job dir).

## Conventions

- **UI strings are Simplified Chinese** (notifications, status, key hints, help screens). **CLI subcommand output is English.** Match this when adding strings.
- Data functions swallow errors and return safe empties (`[]`, `{}`, `False`, `None`) rather than raising — the TUI must never crash on a malformed transcript or missing tmux/claude.
- Destructive cleanup always previews first: `_enter_preview` shows targets in an `Overlay`, `_confirm_cleanup` executes on a second `Enter`.
- Config is a single global `cfg = Config()` in `config.py`; tests override paths by monkeypatching `cfg` attributes (e.g. `cfg.claude_home`, `cfg.workspace`).

## Trellis

This repo is managed by Trellis (see `AGENTS.md`). The development workflow, coding specs, and task tracking live under `.trellis/` (`workflow.md`, `spec/`, `tasks/`). Slash commands like `/trellis:continue` and `/trellis:finish-work` may be available. The `.trellis/`, `.agents/`, and `.codex/` directories are scaffolding for AI agents, not application code.
