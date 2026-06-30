# cc-session-control

TUI manager for [Claude Code](https://claude.ai/code) sessions and Remote Control.

**CLI command: `csctl`**

## Features

- **Sessions Tab** — View, resume, terminate, and delete Claude Code sessions across all projects
- **Remote Control Tab** — Start/stop RC servers per project, toggle auto-start, show running/stopped/dead states
- **Cleanup Tab** — Prune empty/short sessions, sweep orphan artifact directories

Built with [urwid](https://urwid.org/).

> **UI language:** Simplified Chinese (notifications and status text). CLI output is in English.

## Requirements

- Python 3.12+
- [Claude Code](https://claude.ai/code) CLI installed and authenticated
- tmux (for Remote Control management)
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
local `~/.claude`, local `tmux`, and local workspace. Install it separately on
each machine whose sessions you want to manage. For working *on* the code
instead of using it, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Usage

```bash
# Open TUI
csctl

# Remote Control management (no TUI)
csctl rc status          # Show all projects and RC status
csctl rc add .           # Add current project to RC list and start
csctl rc add myproject   # Add by name
csctl rc rm myproject    # Remove and stop
csctl rc up              # Start all listed projects
csctl rc stop all        # Stop all RC servers
csctl rc list            # Show auto-start list

# Session cleanup
csctl prune                          # Dry run: show stats
csctl prune --max-prompts 1 --apply  # Delete sessions with ≤1 prompt

# Options
csctl --workspace ~/projects   # Override workspace root
csctl --version
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `CSCTL_WORKSPACE` | `~/workspace` | Workspace root directory |
| `CSCTL_RC_SESSION` | `rc` | tmux session name for RC servers |
| `CSCTL_RC_STAGGER` | `2` | Seconds between starting RC servers |
| `XDG_CONFIG_HOME` | `~/.config` | Config directory base |

RC auto-start list is stored at `$XDG_CONFIG_HOME/csctl/rc-enabled`.

## License

MIT
