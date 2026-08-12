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
  (`Enter` resumes into a project-labelled window in the shared `csctl` tmux
  session via each CLI's native resume command; `t` bare-terminal fallback;
  `R` backgrounds into tmux;
  `f` forks where the CLI supports it; ⧉ marks tmux-resident sessions),
  terminate, and delete; a cleanup submenu (`c`) prunes empty/short Claude
  sessions and sweeps orphan artifact directories, zombie session files, and
  aged global entries (cleanup models Claude state only)
- **Projects Tab** — The startup tab / launcher: `Enter` opens a CLI chooser
  (active providers only, claude focused first — so Enter-Enter starts
  claude) to open a new project-labelled window in the shared `csctl` tmux
  session; `x`/`k` jump
  straight to codex/kimi; start/stop
  Claude RC servers per project (`o`/`s`), toggle per-project auto Remote
  Control (`c`), show running/stopped/dead states
- **Background agents Tab** — List Claude Code background agent jobs; take
  over, respawn, watch their timeline, stop, or remove them

Non-Claude liveness is deliberately conservative (ADR-0005). A real resume
target in argv (`codex resume <sid-or-unique-name>` / `kimi --session <sid>`)
has priority; csctl's own identity-checked tmux dispatch metadata is a
supplement for dispatched windows, and is essential after Kimi rewrites its
argv at runtime. A brand-new session started from the launcher (the `Enter`
chooser or `x`/`k`) has no sid yet, so it stays unbound, as does any other
bare-launched TUI; unbound processes are never stop/takeover targets.

Kimi can close that gap opt-in via its official hooks: add this to
`~/.kimi-code/config.toml` and every kimi session — bare-launched ones
included — self-reports its pid↔session binding (verified on Kimi Code
0.34.0; csctl re-verifies pid identity and process start time per entry, so
stale or forged entries never bind):

```toml
[[hooks]]
event = "SessionStart"
command = "csctl _kimi-hook"
timeout = 5

[[hooks]]
event = "SessionEnd"
command = "csctl _kimi-hook"
timeout = 5
```

The hook fires when a session materializes (a TUI's first prompt), and the
binding appears on csctl's next refresh.

All agent sessions csctl dispatches — new, resumed, forked, backgrounded, or
background-agent respawns — share the tmux session named `csctl`; their window
names retain the project. Existing live sessions in older or user-created tmux
sessions are entered in place and are never migrated automatically. Managed
Remote Control servers remain isolated in the configurable `rc` session.

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
- Linux / WSL. Other POSIX systems are not supported; without `/proc`, csctl
  shows degraded state and refuses destructive actions whose safety it cannot
  prove.

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

`csctl` manages local state on the machine where it is installed: the active
providers' `~/.claude`, `~/.codex`, and `~/.kimi-code` homes, local `tmux`, and
the project launcher entries recorded in `~/.claude.json`. Install it separately
on each machine whose sessions you want to manage. For working *on* the code
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
| `CSCTL_RC_SESSION` | `rc` | Literal tmux session name for RC servers (target syntax `= : * ? [ ] $ @ %` is rejected); must differ from the reserved workbench session `csctl` |
| `CSCTL_CLEANUP_AGE_DAYS` | `14` | Minimum age in days for the age sweep in the Sessions cleanup submenu (must be an integer ≥ 0) |
| `CSCTL_THEME` | `auto` | TUI palette: `auto` (detect the terminal background via `$COLORFGBG`, else `dark`) / `dark` / `light`. Most terminals (including tmux) don't set `$COLORFGBG`, so `auto` falls back to `dark` — set this (or `--theme`) explicitly for a light terminal |

## License

MIT
