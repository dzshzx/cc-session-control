# cc-session-control

tmux-first workbench (TUI + headless CLI) for the agent CLIs on one machine
— [Claude Code](https://claude.ai/code), [Codex CLI](https://github.com/openai/codex),
and [Kimi Code](https://github.com/MoonshotAI/kimi-code): sessions across
all three, plus Claude Code background agents and Remote Control.

**CLI command: `csctl`**

## Features

- **Sessions Tab** — One machine-wide list of Claude Code, Codex, and Kimi
  Code sessions (CLI column: `cc`/`cx`/`km`), discovered from each CLI's own
  on-disk state — not just sessions csctl started. Resume tmux-first
  (`Enter` resumes into the per-project tmux window via each CLI's native
  resume command; `t` bare-terminal fallback; `R` backgrounds into tmux;
  `f` forks where the CLI supports it; ⧉ marks tmux-resident sessions),
  terminate, and delete; a cleanup submenu (`c`) prunes empty/short Claude
  sessions and sweeps orphan artifact directories, zombie session files, and
  aged global entries (cleanup models Claude state only)
- **Projects Tab** — The startup tab / launcher: start a new tmux session in
  a project dir with claude (`Enter`), codex (`x`), or kimi (`k`); start/stop
  Claude RC servers per project (`o`/`s`), toggle per-project auto Remote
  Control (`c`), show running/stopped/dead states
- **Background agents Tab** — List Claude Code background agent jobs; take
  over, respawn, watch their timeline, stop, or remove them

Non-Claude liveness is deliberately conservative (ADR-0005): a codex/kimi
process is bound to its session only when its argv carries the session id
(`codex resume <sid>` / `kimi --session <sid>`) — which is how csctl
dispatches an EXISTING session back into tmux. A brand-new session started
from the launcher (`x`/`k`) is bare argv with no session id yet, so it is
never bound either, same as any other bare-launched TUI; neither is ever a
stop/takeover target.

Built with [urwid](https://urwid.org/).

> **UI language:** Simplified Chinese (notifications and status text). CLI output is in English.

## Requirements

- Python 3.12+
- At least one supported agent CLI installed and authenticated:
  [Claude Code](https://claude.ai/code), [Codex CLI](https://github.com/openai/codex),
  and/or [Kimi Code](https://github.com/MoonshotAI/kimi-code) — each is
  discovered automatically when its state home exists (`~/.claude`,
  `~/.codex`, `~/.kimi-code`; official relocation variables `CODEX_HOME` /
  `KIMI_CODE_HOME` are honored)
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

# Resume rescue (headless): list sessions of ALL providers across
# directories with ready-to-copy resume commands (native /resume only
# searches the cwd and hides sdk-ts/bridge sessions); non-Claude rows
# are tagged [codex]/[kimi]
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
| `CSCTL_PROVIDERS` | `claude,codex,kimi` | Comma list of allowed agent-CLI providers; a listed provider is active only when its state home also exists |
| `CSCTL_RC_SESSION` | `rc` | tmux session name for RC servers |
| `CSCTL_CLEANUP_AGE_DAYS` | `14` | Minimum age in days for the age sweep in the Sessions cleanup submenu (must be an integer ≥ 0) |
| `CSCTL_THEME` | `auto` | TUI palette: `auto` (detect the terminal background via `$COLORFGBG`, else `dark`) / `dark` / `light`. Most terminals (including tmux) don't set `$COLORFGBG`, so `auto` falls back to `dark` — set this (or `--theme`) explicitly for a light terminal |

## License

MIT
