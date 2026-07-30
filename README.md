# cc-session-control

tmux-first TUI and headless CLI for [Claude Code](https://claude.ai/code)
sessions, background agents, and Remote Control.

**CLI command: `csctl`**

## Features

- **Sessions Tab** — View, resume (tmux-first: `Enter` resumes into the per-project tmux window; `t` bare-terminal fallback; `R` backgrounds into tmux; ⧉ marks tmux-resident sessions), terminate, and delete Claude Code sessions across all projects; a cleanup submenu (`c`) prunes empty/short sessions and sweeps orphan artifact directories
- **Projects Tab** — The startup tab: start a new tmux claude session in a project dir (`Enter`), start/stop RC servers per project (`o`/`s`), toggle auto-start, show running/stopped/dead states
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

```bash
# Open TUI
csctl

# Remote Control management (no TUI)
csctl rc status          # Show all projects and RC status
csctl rc add .           # Add current directory to RC list and start
csctl rc add ~/code/app  # Add by directory path
csctl rc rm ~/code/app   # Remove and stop
csctl rc up              # Start all listed projects
csctl rc stop all        # Stop all RC servers
csctl rc list            # Show auto-start list

# Session cleanup
csctl prune                          # Dry run: show stats
csctl prune --max-prompts 1 --apply  # Delete sessions with ≤1 prompt
csctl prune --sweep-orphans          # Dry run: orphan sid-keyed artifact dirs
csctl prune --sweep-zombies          # Dry run: dead sessions/<pid>.json files
csctl prune --sweep-aged             # Dry run: age-keyed global entries
# Add --apply to exactly one of the sweep commands above to execute it.

# Resume rescue (headless): list sessions across directories with
# ready-to-copy resume commands (native /resume only searches the cwd
# and hides sdk-ts/bridge sessions)
csctl resume                 # Page 1, 20 per page
csctl resume mybug           # Keyword: sid/cwd/title, then transcript body
csctl resume --page 2        # Next page
csctl resume --all           # Everything, no paging

# Read-only inventory
csctl agents                 # Background agents: state, tempo, name, cwd

# Bundled Claude Code skill (session-doctor knowledge for the agent)
csctl skill install          # Write SKILL.md to ~/.claude/skills/
csctl skill install --force  # Replace an existing skill directory
csctl skill uninstall

# Options
csctl --theme light            # Force the TUI palette (auto/dark/light)
csctl --version
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `CSCTL_RC_SESSION` | `rc` | tmux session name for RC servers |
| `CSCTL_RC_STAGGER` | `2` | Seconds between starting RC servers |
| `CSCTL_CLEANUP_AGE_DAYS` | `14` | Minimum age in days for `csctl prune --sweep-aged` (must be an integer ≥ 1) |
| `CSCTL_THEME` | `auto` | TUI palette: `auto` (detect the terminal background via OSC 11 / `$COLORFGBG`) / `dark` / `light`. tmux typically doesn't answer the OSC 11 query, so inside tmux `auto` falls back to `dark` — set this (or `--theme`) explicitly for a light terminal |
| `XDG_CONFIG_HOME` | `~/.config` | Config directory base |

The RC auto-start list is stored at
`$XDG_CONFIG_HOME/csctl/rc-enabled`.

## License

MIT
