# cc-session-control

tmux-first workbench (TUI + headless CLI) for the agent CLIs on one machine
— [Claude Code](https://claude.ai/code), [Codex CLI](https://github.com/openai/codex),
[Kimi Code](https://github.com/MoonshotAI/kimi-code), and
[opencode](https://github.com/sst/opencode): one session list
across all four, plus a tmux-first project launcher.

**CLI command: `csctl`**

## Features

- **Sessions Tab** — One machine-wide list of Claude Code, Codex, Kimi
  Code, and opencode sessions (CLI column: `cc`/`cx`/`km`/`oc`), discovered
  from each CLI's own
  on-disk state — not just sessions csctl started. Resume tmux-first
  (`Enter` resumes into a CLI-named window in the shared `csctl` tmux
  session via each CLI's native resume command; `t` bare-terminal fallback;
  `R` backgrounds into tmux;
  `f` forks where the CLI supports it; ⧉ marks tmux-resident sessions;
  Codex app-server-held rows show as hosted/read-only instead of dead),
  terminate, and delete; `y` copies the resume command to the clipboard;
  `a` shows/hides archived Codex sessions (hidden by default since 0.8.14);
  a cleanup submenu (`c`) prunes empty/short Claude
  sessions and sweeps orphan artifact directories, zombie session files, and
  aged global entries (cleanup models Claude state only)
- **Projects Tab** — The startup tab / launcher: member directories are
  discovered from evidence tiers (operator pins, any CLI's trust records, any
  CLI's session activity). `Enter` opens a CLI chooser
  (active providers only, claude focused first — so Enter-Enter starts
  claude) to open a new CLI-named window in the shared `csctl` tmux
  session; `x`/`k`/`O` jump
  straight to codex/kimi/opencode; `p`/`h`/`H` pin, hide, and reveal projects

Non-Claude liveness is deliberately conservative (ADR-0005). A real resume
target in argv (`codex resume <sid-or-unique-name>` / `kimi --session <sid>`
/ `opencode --session <sid>`)
has priority; csctl's own identity-checked tmux dispatch metadata is a
supplement for dispatched windows, and is essential after Kimi rewrites its
argv at runtime. A brand-new session started from the launcher (the `Enter`
chooser or `x`/`k`/`O`) has no sid yet, so it stays unbound, as does any other
bare-launched TUI; unbound processes are never stop/takeover targets.
Codex app-server fd evidence is a separate hosted/read-only state: it never
supplies a session pid, and csctl emits no resume/stop/fork/delete command for
that row.

Kimi can close that gap opt-in via its official hooks: add this to
`~/.kimi-code/config.toml` and every kimi session — bare-launched ones
included — self-reports its pid↔session binding, which csctl re-verifies
against `/proc` before trusting it:

```toml
[[hooks]]
event = "SessionStart"
command = "csctl _kimi-hook"
timeout = 5

[[hooks]]
event = "SessionEnd"
command = "csctl _kimi-hook"
timeout = 5

# Self-heal: re-registers the live session every 60 s, so a SessionStart
# kimi never delivered becomes a ≤60 s unbound window instead of a lifetime.
[[hooks]]
event = "SessionHeartbeat"
command = "csctl _kimi-hook"
timeout = 5
```

The hooks are needed because kimi rewrites away its own argv at runtime and
creates a new session's id only at its first prompt, so neither argv nor
csctl's dispatch metadata can bind every session. The kimi versions this was
verified on and the registration timing per launch path are recorded in
[docs/claude-code-compatibility.md](docs/claude-code-compatibility.md); the
`run/hook-errors.log` diagnostics and the full evidence trail are in
[ADR-0005](docs/adr/0005-multi-cli-provider-layer.md).

All agent sessions csctl dispatches — new, resumed, forked, or backgrounded —
share the tmux session named `csctl`; each window is named for the bare CLI
(`claude`/`codex`/`kimi`/`opencode`, or `codex-<label>` for a declared codex
identity) — never the project or sid. Existing live sessions in older or
user-created tmux sessions are entered in place and are never migrated
automatically.

For phone terminals, the managed `csctl` tmux session gets a scoped second
prefix when none is already configured: press `Ctrl-A`, then `s`, to open
tmux's session tree from inside Claude, Codex, or Kimi. An existing `prefix2`
is preserved; csctl never changes the primary prefix or global tmux config.

Built with [urwid](https://urwid.org/).

> **UI language:** Simplified Chinese (notifications and status text). CLI output is in English.

## Requirements

- Python 3.12+
- At least one supported agent CLI installed and authenticated:
  [Claude Code](https://claude.ai/code), [Codex CLI](https://github.com/openai/codex),
  [Kimi Code](https://github.com/MoonshotAI/kimi-code), and/or
  [opencode](https://github.com/sst/opencode) — each is
  discovered automatically when its state home exists (`~/.claude`,
  `~/.codex`, `~/.kimi-code`, `~/.local/share/opencode`; official relocation
  variables `CODEX_HOME` / `KIMI_CODE_HOME` are honored, and opencode's data
  home follows `XDG_DATA_HOME`). Several codex identities on one machine are
  supported by declaring their homes — see Configuration below
- tmux (the primary session-lifecycle carrier: launch, resume, background, and
  survive terminal/SSH disconnects)
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

To try the newest `master` before it is released, install from GitHub
(`--reinstall` forces a rebuild — a plain `uv tool upgrade` keeps the cached
git ref):

```bash
uv tool install --reinstall git+https://github.com/dzshzx/cc-session-control.git
```

`csctl` manages local state on the machine where it is installed: the active
providers' `~/.claude`, `~/.codex`, `~/.kimi-code`, and
`~/.local/share/opencode` homes, local `tmux`, and
the project launcher entries recorded in `~/.claude.json`. Install it separately
on each machine whose sessions you want to manage. For working *on* the code
instead of using it, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Usage

The TUI is the primary surface — the project launcher and session cleanup
live there (Projects tab and the Sessions cleanup submenu). The
headless CLI keeps only the agent-facing command: `resume`.

```bash
# Open TUI
csctl

# Resume rescue (headless): list sessions of ALL providers across
# directories with ready-to-copy resume commands (native /resume only
# searches the cwd and hides sdk-ts/bridge sessions); non-Claude rows
# are tagged [codex]/[kimi]/[opencode]
csctl resume                 # Page 1, 20 per page
csctl resume mybug           # Keyword: sid/cwd/title, then transcript body
csctl resume --page 2        # Next page
csctl resume --all           # Everything, no paging
csctl resume --take-over <sid>  # Claude only: re-resolve at execution time, then guarded live takeover

# Options
csctl --theme light            # Force the TUI palette (auto/dark/light)
csctl --version
```

Session rescue is built into csctl itself: use `csctl resume` or the TUI.

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `CSCTL_PROVIDERS` | `claude,codex,kimi,opencode` | Comma list of allowed agent-CLI providers; a listed provider is active only when its state home also exists |
| `CSCTL_CLEANUP_AGE_DAYS` | `14` | Minimum age in days for the age sweep in the Sessions cleanup submenu (must be an integer ≥ 0) |
| `CSCTL_THEME` | `auto` | TUI palette: `auto` (detect the terminal background via `$COLORFGBG`, else `dark`) / `dark` / `light`. Most terminals (including tmux) don't set `$COLORFGBG`, so `auto` falls back to `dark` — set this (or `--theme`) explicitly for a light terminal |

### Multiple codex identities

`CODEX_HOME` says which home *one codex process* uses, and it is inherited by
everything that process spawns — including csctl. Reading it as the machine's
inventory made csctl show only the identity it happened to be launched from.
So when you run more than one codex identity, declare their homes in
`~/.config/csctl/providers.json` (XDG-respecting) instead:

```json
{
  "codex_homes": [
    {"label": "cx",  "home": "~/.codex"},
    {"label": "cx2", "home": "~/.codex-eva02", "env_file": "~/.codex-eva02/launch.env"}
  ]
}
```

That list is then the complete codex inventory and `CODEX_HOME` no longer
takes part, so csctl shows the same machine state from any terminal. Each
entry becomes its own provider — separate sessions, trust records, badges,
and launcher entry — tagged in the CLI column by its `label` (ASCII
alphanumeric, ≤3 characters). The first entry keeps the provider key `codex`;
later ones become `codex:<label>`. Every command csctl synthesizes for a
declared identity carries its `CODEX_HOME` explicitly. Without this file
there is exactly one codex instance following `CODEX_HOME`/`~/.codex`, and a
malformed file falls back to that same single instance with a visible reason.
An optional `env_file` (ADR-0012) names a `KEY=value` file whose variables csctl injects
only into the processes it spawns for that identity (new / resume / fork) — never
persisted into the home, never shown in the TUI, never part of the clipboard command;
the file is re-read at every spawn. Changes take effect on the next csctl start. See
[ADR-0008](docs/adr/0008-declared-cli-instances.md) and
[ADR-0012](docs/adr/0012-per-instance-launch-env-file.md).

## License

MIT
