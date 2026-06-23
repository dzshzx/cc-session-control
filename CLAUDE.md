# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cc-session-control` (CLI: `csctl`) is a terminal UI that manages **Claude Code's own** sessions and Remote Control servers on the local machine. It reads Claude Code's on-disk state (`~/.claude/projects/*/*.jsonl` transcripts, `~/.claude.json`) and shells out to the `claude` CLI and `tmux` — it is an operator tool *for* Claude Code, not a general app. The TUI has two tabs: **会话 (Sessions)** and **远程控制 (Remote Control)**; cleanup is a submenu inside Sessions, not a tab.

## Commands

```bash
# Dev install (editable, requires Python 3.12+)
uv venv && uv pip install -e ".[dev]"

# Run the TUI
csctl

# Tests
python -m pytest tests/                                                # all
python -m pytest tests/test_views.py::test_sessions_view_filter_logic  # single test

# Guardrail enforced for contributions (must return nothing)
grep -rn '/home/' src/      # no hardcoded paths
```

Constraints from `CONTRIBUTING.md`: keep each source file **under 600 lines**, use type hints, no hardcoded paths.

## Architecture

The UI toolkit is **urwid** (the only runtime dependency is `urwid>=2.0.0`). Three layers under `src/cc_session_control/`:

- **`data/`** — everything that touches external state, both reads *and* writes: `sessions.py` (parse transcripts, prune/remove), `rc.py` (tmux + trust state, start/stop RC), `agents.py` (liveness). Returns the dataclasses in `models.py` (`Session`, `RCProject`).
- **`actions/session_ops.py`** — a small set of session-level operations that don't belong in `data/`: `terminate_session`, `resume_cmd`/`do_resume`, `to_clipboard`.
- **`views/`** — urwid widgets per tab (`sessions.py`, `rc.py`). `app.py` orchestrates them; `cli.py` is the argparse entry point; `config.py` holds the global `cfg` singleton.

The invariant is **import direction, not purity**: `views` import from `data` and `actions`; `data`/`actions` never import upward. There is no separate "pure read vs side effect" split — `data/` holds both.

### The view contract (how `app.py` drives tabs generically)

`App` holds `self.views = [SessionsView(self), RCView(self)]` and treats each via a duck-typed interface. To add/modify a tab, honor these members:

- `.widget` — the urwid widget for the tab body
- `._loaded` — bool; whether `load()` has run
- `load()` — synchronous initial scan + first render (called once in `run()` for the startup tab; switching to an as-yet-unloaded tab triggers an async refresh instead)
- `fetch_pending()` — **runs on the worker thread**; scans and stashes results in `self._pending` (never touches widgets)
- `apply_data()` — **runs on the main loop**; swaps `_pending` into the live walker
- `keyhints() -> str` — footer hint string for the current mode
- `handle_key(key)` — handles every key except `Tab` and `q`

`App._input` handles only `tab` (switch) and `q` (quit); everything else is forwarded to the active view's `handle_key`.

### Async refresh (the threading model)

Scanning hits the filesystem and subprocesses, so it must not block the urwid loop. The pattern (`App.trigger_async_refresh`):

1. A daemon thread calls `view.fetch_pending()` on each view — these write into each view's `_pending` field and **never touch widgets directly**.
2. The thread writes one byte to a pipe registered via `loop.watch_pipe(self._on_pipe)`.
3. `_on_pipe` runs on the main loop and calls `apply_data()` on each view, which swaps `_pending` into the live walker.

A `self._refreshing` guard prevents overlapping refreshes. Auto-refresh re-arms every 10s via `set_alarm_in`. **Never mutate urwid widgets from the worker thread** — only set `_pending`.

### Resume happens *outside* the UI loop (process replacement)

The TUI cannot run `claude` inside itself. To resume a session, `SessionsView` calls `app.exit_with_resume(session, fork)`, which exits the MainLoop returning a `("resume", session, fork)` tuple. Back in `cli._cmd_tui`, `do_resume` (in `actions/session_ops.py`) then `os.chdir`s to the session's cwd and `os.execvp("claude", ...)` — **replacing the csctl process**. This is why the final resume step lives in `cli.py`, not the view.

**Gotcha — intentional divergence:** `resume_cmd` (the copy-to-clipboard string, `y` key) emits a `kill <pid> && sleep 1` prefix whenever the session is alive & not current, *regardless of fork*; `do_resume` (the actual exec) skips the kill when `fork=True`. This inconsistency is deliberately preserved (see the `DIVERGENCE` comments + `test_resume_cmd_fork_while_alive_keeps_kill_prefix`). Don't "fix" it casually — it's a known latent item awaiting a real behavior-change task.

### Session liveness — single source of truth

`data/agents.py::alive_map()` is the **one authority** for which sessions are alive. It runs `claude agents --json` (cached 5s) → `{sessionId: pid}`. A session is "alive" iff its id appears there. After any operation that changes liveness (terminate/delete/cleanup), call `invalidate_cache()`.

`data/sessions.py::_ancestor_pids()` walks `/proc/<pid>/stat` up the parent chain to find csctl's own ancestor PIDs. A session whose pid is in that set is the **"current"** session (the one that launched csctl) and is protected — you cannot resume, terminate, or prune it. This `/proc` walk is **Linux/WSL only**; liveness degrades on macOS.

### Session model & transcript parsing

`sessions.scan()` globs `~/.claude/projects/*/*.jsonl` and line-scans each (a cheap substring pre-check guards every `json.loads` for speed — keep this pattern). Display `label` priority: `aiTitle` → first non-noise user prompt → `lastPrompt` → `(untitled)`. `_NOISE` / `_clean_text` strip command/system-reminder wrapper tags so prompts read cleanly. `hidden` flags sdk-ts / bridge-session transcripts.

### Remote Control = tmux windows

RC servers are **tmux windows** in a session named `rc` (env `CSCTL_RC_SESSION`). `rc.start_one` launches a bash loop that re-runs `claude remote-control --name ws/<proj> --spawn same-dir` with **exponential backoff** (5s→60s, reset to 5s if the process ran ≥120s). Status comes from tmux `#{pane_dead}`: `running` / `dead` / `stopped`.

All tmux access goes through a single seam: only `_tmux_run` touches `subprocess`; every other tmux call is a thin verb wrapper (`_tmux_new_window`, `_tmux_kill_window`, …) that keeps the swallow-errors contract. Add new tmux operations as wrappers, not raw `subprocess` calls.

**Two independent "start" concepts — do not conflate them** (both are columns in the RC tab):
- `auto_start` ("自启") — project is in csctl's own list at `$XDG_CONFIG_HOME/csctl/rc-enabled`; controls what `csctl rc up` / the `A` key starts.
- `rc_at_startup` ("接管") — the per-project `remoteControlAtStartup` flag in `<proj>/.claude/settings.local.json`; controls whether **`claude` itself** enables Remote Control on launch. Tri-state (`True`/`False`/`None`=unset).

A project must be **trusted** (`hasTrustDialogAccepted` in `~/.claude.json`) before RC can start.

## Conventions

- **UI strings are Simplified Chinese** (notifications, status, key hints, help screens). **CLI subcommand output is English.** Match this when adding strings.
- Data functions swallow errors and return safe empties (`[]`, `{}`, `False`, `None`) rather than raising — the TUI must never crash on a malformed transcript or missing tmux/claude.
- Destructive cleanup always previews first: `_enter_preview` shows targets in an `Overlay`, `_confirm_cleanup` executes on a second `Enter`.
- Config is a single global `cfg = Config()` in `config.py`; tests override paths by monkeypatching `cfg` attributes (e.g. `cfg.claude_home`, `cfg.workspace`).

## Trellis

This repo is managed by Trellis (see `AGENTS.md`). The development workflow, coding specs, and task tracking live under `.trellis/` (`workflow.md`, `spec/`, `tasks/`). Slash commands like `/trellis:continue` and `/trellis:finish-work` may be available. The `.trellis/`, `.agents/`, and `.codex/` directories are scaffolding for AI agents, not application code.
