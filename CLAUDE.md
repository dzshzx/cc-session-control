# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cc-session-control` (CLI: `csctl`) is a terminal UI that manages **Claude Code's own** sessions and Remote Control servers on the local machine. It reads Claude Code's on-disk state (`~/.claude/projects/*.jsonl` transcripts, `~/.claude.json`) and shells out to the `claude` CLI and `tmux` — it is an operator tool *for* Claude Code, not a general app.

## Commands

```bash
# Dev install (editable)
uv venv && uv pip install -e ".[dev]"

# Run the TUI
csctl

# Tests
python -m pytest tests/                              # all
python -m pytest tests/test_views.py::test_sessions_view_filter_logic   # single test

# Guardrail enforced for contributions
grep -rn '/home/' src/      # must return nothing (no hardcoded paths)
```

Constraints from `CONTRIBUTING.md`: keep each source file **under 600 lines**, use type hints, no hardcoded paths.

## Architecture

The UI toolkit is **urwid** (not Textual — README/PKG-INFO say Textual, but that is stale; the only dependency is `urwid>=2.0.0` and all view code is urwid). Three layers, strictly one-directional (`views → actions → data`):

- **`data/`** — pure functions that read/scan external state and return dataclasses (`models.py`). No UI. `sessions.py` (parse transcripts), `rc.py` (tmux + trust state), `agents.py` (liveness).
- **`actions/`** — side-effecting operations (resume, terminate, delete, start/stop RC). Thin wrappers over `data/`, called from views.
- **`views/`** — urwid widgets for each tab. `app.py` orchestrates them; `cli.py` is the argparse entry point.

### The view contract (how `app.py` drives tabs generically)

`App` holds `self.views = [SessionsView, RCView]` and treats each via a duck-typed interface — to add/modify a tab, honor these members: `.widget`, `._loaded`, `load()`, `set_pending(data)`, `apply_data()`, `keyhints() -> str`, `handle_key(key)`. The `App` itself only handles `Tab` (switch) and `q` (quit); every other key is forwarded to the active view's `handle_key`.

### Async refresh (the threading model)

Scanning hits the filesystem and subprocesses, so it must not block the urwid loop. The pattern (`App.trigger_async_refresh`):
1. A daemon thread runs the scans and writes results into each view's `_pending` field (never touches widgets directly).
2. The thread writes one byte to a pipe registered via `loop.watch_pipe(self._on_pipe)`.
3. `_on_pipe` runs on the main loop and calls `apply_data()` on each view, which swaps `_pending` into the live widget walker.

Auto-refresh fires every 10s via `set_alarm_in`. **Never mutate urwid widgets from the worker thread** — only set `_pending`.

### Resume happens *outside* the UI loop (process replacement)

The TUI cannot run `claude` inside itself. To resume a session, `SessionsView` calls `app.exit_with_resume(...)`, which exits the MainLoop returning a `("resume", session, fork)` tuple. Back in `cli._cmd_tui`, `do_resume` then `os.chdir`s to the session's cwd and `os.execvp("claude", ...)` — **replacing the csctl process**. This is why resume logic lives in `cli.py`, not the view.

### Session liveness — single source of truth

`data/agents.py::alive_map()` is the **one authority** for which sessions are alive. It runs `claude agents --json` (cached 5s) → `{sessionId: pid}`. A session is "alive" iff its id appears there. After any operation that changes liveness (terminate/delete/cleanup), call `invalidate_cache()`.

`data/sessions.py::_ancestor_pids()` walks `/proc/<pid>/stat` up the parent chain to find csctl's own ancestor PIDs. A session whose pid is in that set is the **"current"** session (the one that launched csctl) and is protected — you cannot resume or terminate it. This `/proc` walk is **Linux/WSL only**; liveness detection degrades on macOS.

### Session model & transcript parsing

`sessions.scan()` globs `~/.claude/projects/*/*.jsonl` and line-scans each (substring pre-check before `json.loads` for speed). Display `label` priority: `aiTitle` → first non-noise user prompt → `lastPrompt` → `(untitled)`. `_NOISE`/`_clean_text` strip command/system-reminder wrapper tags so prompts read cleanly. `hidden` flags sdk-ts / bridge-session transcripts.

### Remote Control = tmux windows

RC servers are **tmux windows** in a session named `rc` (env `CSCTL_RC_SESSION`). `rc.start_one` launches a bash loop that re-runs `claude remote-control --name ws/<proj> --spawn same-dir` with **exponential backoff** (5s→60s, reset to 5s if the process ran ≥120s). Status is read from `tmux ... #{pane_dead}`: `running` / `dead` / `stopped`.

**Two independent "start" concepts — do not conflate them** (both are columns in the RC tab):
- `auto_start` ("自启") — project is in csctl's own list at `$XDG_CONFIG_HOME/csctl/rc-enabled`; controls what `csctl rc up` / the `A` key starts.
- `rc_at_startup` ("接管") — the per-project `remoteControlAtStartup` flag in `<proj>/.claude/settings.local.json`; controls whether **`claude` itself** enables Remote Control on launch. Tri-state (`True`/`False`/`None`=unset).

A project must be **trusted** (`hasTrustDialogAccepted` in `~/.claude.json`) before RC can start.

## Conventions

- **UI strings are Simplified Chinese** (notifications, status, key hints, help screens). **CLI subcommand output is English.** Match this when adding strings.
- Data functions swallow errors and return safe empties (`[]`, `{}`, `False`) rather than raising — the TUI must never crash on a malformed transcript or missing tmux.
- Destructive cleanup always previews first: `_enter_preview` shows targets in an Overlay, `_confirm_cleanup` executes on a second Enter.

## Known stale spots (safe to ignore / clean up)

- `src/cc_session_control/views/cleanup.py` (`CleanupView`) is **dead code**. The cleanup tab was merged into `SessionsView` as the `c` submenu; nothing imports `CleanupView`. Edit the cleanup logic inside `views/sessions.py`, not here.
- README.md and the generated `PKG-INFO` claim "Built with Textual" — incorrect; it's urwid.
