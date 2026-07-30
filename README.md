# cc-session-control

tmux-first TUI and headless CLI for [Claude Code](https://claude.ai/code)
sessions, background agents, and Remote Control.

**CLI command: `csctl`**

## Features

- **Sessions Tab** — View, resume (tmux-first: `Enter` resumes into the per-project tmux window; `t` bare-terminal fallback; `R` backgrounds into tmux; ⧉ marks tmux-resident sessions), terminate, and delete Claude Code sessions across all projects; a cleanup submenu (`c`) prunes empty/short sessions and sweeps orphan artifact directories
- **Projects Tab** — The startup tab: start a new tmux claude session in a project dir (`Enter`), start/stop RC servers per project (`o`/`s`), toggle per-project auto Remote Control (`c`), show running/stopped/dead states
- **Background agents Tab** — List background agent jobs; take over, respawn, watch their timeline, stop, or remove them

Built with [urwid](https://urwid.org/).

> **UI language:** Simplified Chinese (notifications and status text). CLI output is in English.

## Requirements

- Python 3.12+
- [Claude Code](https://claude.ai/code) CLI installed and authenticated
- tmux (the primary session-lifecycle carrier: launch, resume, background, and
  survive terminal/SSH disconnects; managed Remote Control servers also use it)
- Linux / WSL (macOS support is partial — `/proc`-based liveness detection is Linux-only)

## Installation

Install the latest published release:

```bash
uv tool install cc-session-control
# or
pipx install cc-session-control
```

Upgrade later with `uv tool upgrade cc-session-control` (or `pipx upgrade
cc-session-control`).

### Latest `master` build

To try the newest `master` before it is released, install from GitHub:

```bash
uv tool install --reinstall git+https://github.com/dzshzx/cc-session-control.git
```

`csctl` manages the Claude Code state on the machine where it is installed: the
local `~/.claude`, local `tmux`, and the projects recorded in the local
`~/.claude.json`. Install it separately on each machine whose sessions you want
to manage. For working *on* the code
instead of using it, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Usage

The TUI is the primary surface — Remote Control management and session
cleanup live there (Projects tab and the Sessions cleanup submenu). The
headless CLI keeps only the agent-facing commands: `resume` and `agents`.

```bash
# Open TUI
csctl

# Resume rescue (headless): list sessions across directories with
# ready-to-copy resume commands (native /resume only searches the cwd
# and hides sdk-ts/bridge sessions)
csctl resume                 # Page 1, 20 per page
csctl resume mybug           # Keyword: sid/cwd/title, then transcript body
csctl resume --page 2        # Next page
csctl resume --all           # Everything, no paging

# Read-only inventory
csctl agents                 # Background agents: state, tempo, name, cwd

# Options
csctl --theme light            # Force the TUI palette (auto/dark/light)
csctl --version
```

The companion Claude Code skill (`claude-session-doctor`) is distributed via
the [skills CLI](https://github.com/dzshzx/agent-skills) — install it with
`skills add dzshzx/agent-skills --skill=claude-session-doctor` (it is no
longer bundled with this package).

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `CSCTL_RC_SESSION` | `rc` | tmux session name for RC servers |
| `CSCTL_CLEANUP_AGE_DAYS` | `14` | Minimum age in days for the age sweep in the Sessions cleanup submenu (must be an integer ≥ 0) |
| `CSCTL_THEME` | `auto` | TUI palette: `auto` (detect the terminal background via `$COLORFGBG`, else `dark`) / `dark` / `light`. Most terminals (including tmux) don't set `$COLORFGBG`, so `auto` falls back to `dark` — set this (or `--theme`) explicitly for a light terminal |

## License

MIT
