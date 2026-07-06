# Changelog

## 0.6.1 (2026-07-06)

UI/UX audit fix batch: honest footers, safe gates, readable modals.

- **R10 degrade gates on every live takeover.** Sessions `Enter` and Agents
  `Enter`/`o` now refuse a live takeover in-TUI when `/proc` is unavailable
  (matching `t`/`R`) instead of confirming, exiting the TUI, and only then
  printing the action-layer refusal. Dead sessions stay resumable.
- **Filter mode no longer leaks across tabs.** New `deactivate()` member on the
  TabView contract: switching tabs commits + closes a transient filter, so keys
  after switching back never edit an invisible footer Edit.
- **Confirm modal sizes to its content** (wrapped-text height, 46-cell floored
  width) instead of fixed 50%×7, and names in confirm messages truncate by
  terminal CELL width (`truncate_cells`, CJK = 2 cells) instead of `[:30]`.
- **Newest notification owns the footer** — an older notify timer can no longer
  clear a newer message early.
- **Footer honesty in modal modes.** `r` now refreshes (via one shared
  `App.refresh_with_notice`) in help/watch/cleanup-preview modes, and the help
  hint reads 其余任意键返回 — every footer segment is true in every mode.
- **RC help is a scrollable overlay** over the intact project list (shared
  `TextRow`), readable on short terminals.
- **Bundled skill key table corrected** — it still documented `t` as
  "terminate"; now matches the real verb table (`t` tmux 接回 / `s` 停止 /
  `R` 转入后台 / `d` 删除 …), locked by a test.
- README features now describe the real 会话 / 项目 / 后台 tabs (cleanup is a
  Sessions submenu, not a tab).

## 0.6.0 (2026-07-06)

The RC tab becomes a project launcher, and tmux organization goes per-project.

- **项目 (Projects) tab** — the 远程控制 tab is renamed: it already listed
  workspace projects, and Remote Control is just one set of verbs on them. New
  `t` key on a project row: start a NEW claude session in that project's
  directory inside tmux and bring your terminal straight into it (no
  `--remote-control`, nothing killed → no confirm). Tab order is now
  会话 / 项目 / 后台.
- **One tmux session per project.** Every claude spawn (`t` 新建, `t` 接回,
  `R` 转入后台, background-agent respawn) now lands in a tmux session named
  after the project directory (`.`/`:` sanitized to `-`) instead of one shared
  `cc` session — `tmux ls` reads as a project list. **Breaking:**
  `CSCTL_TMUX_SESSION` is removed. Existing windows in the old `cc` session
  are unaffected (`t` on a tmux-hosted session still enters it in place).
- **Exact tmux targets.** `run_in_tmux` returns the spawned window's
  `session:window_index` (tmux `-P`), so entering a window no longer guesses
  by name when names collide.
- **Env ledger left the TUI.** The 项目 tab no longer renders the
  bridge-environment ledger (csctl cannot deregister cloud environments); the
  ledger still records every cycle and `csctl env` remains the query surface.
  The read-only RC server section stays.
- **Enter confirms the kill-confirm modal** alongside `y` (`n`/`Esc` cancel,
  unchanged) — standard dialog muscle memory.

## 0.5.1 (2026-07-05)

Liveness false-positive fixes, a new foreground-tmux resume tier, and a
spec-driven TUI polish pass.

- **Fix: sessions no longer show as running after they stopped.** Two holes in
  the `claude agents --json` merge are closed: pid-less entries (settled bg
  sessions the CLI keeps listing) no longer count as alive, and entries whose
  pid is dead (`/proc/<pid>` gone before claude's registry caught up) are
  scrubbed at cache-refresh time. Degraded (no-`/proc`) platforms keep the old
  behavior — there the agents list is the only liveness source.
- **`t` key — tmux 接回 (foreground tmux resume).** The middle tier between
  `Enter` (bare-terminal resume, dies with the terminal) and `R` (background +
  remote control): resumes the session inside a `cc` tmux window (plain resume,
  no `--remote-control`, so no cloud-env entries pile up) and brings your
  terminal into it — a dropped SSH/phone connection no longer kills the
  session. Sessions already living in a tmux pane are entered in place (no
  kill, no confirm); live bare-terminal sessions go through the usual takeover
  confirm. Outside tmux it execs `tmux attach`; inside it switches the client
  and csctl exits.
- **Footer now lists the full key table per tab** (wrapping to extra rows on
  narrow terminals) instead of a trimmed high-frequency subset; `?` keeps the
  detailed semantics.
- **TUI polish per frontend design constraints:** session state is
  triple-encoded (`▸● 忙` / `● 闲` / `○ 停` — shape + word + color, busy/idle
  now visible), crashed RC panes show `✖ 已退出` in red, numeric/time columns
  right-align, uniform 2-cell column gutters, palette collapsed to one
  semantic set with ≥4.5:1 contrast foregrounds, and each tab's header/data
  columns are generated from a single spec so they can never drift.

## 0.5.0 (2026-07-02)

Absorbs the standalone "claude-session-doctor" skill scripts into csctl proper.

- **`csctl resume [keyword]`** — headless resume rescue: lists sessions across
  all project directories (including sdk-ts/bridge sessions the native
  `/resume` picker hides) and prints ready-to-copy resume commands with the
  correct `cd` prefix. Dead sessions get `cd … && claude --resume <id>`; live
  sessions get the takeover form (`kill <pid> && sleep 1 && …`); the session
  csctl runs inside is flagged and never given a kill command. Keyword matches
  sid/cwd/title first, then falls back to scanning the transcript body.
  Paged (`--page` / `--limit`, default 20 per page) with `--all` to disable.
- **`csctl skill install|uninstall`** — the package now bundles a Claude Code
  agent skill (SKILL.md with the session mental model, resume rules, and
  cleanup guidance, routed to csctl subcommands). Install is explicit — no
  postinstall side effects; an existing skill directory is only replaced with
  `--force`.

## 0.4.1 (2026-06-30)

- Docs: drop the "Coming soon to PyPI" banner from the README now that the
  package is published, and reword the "Latest `master` build" section. This
  release exists mainly to refresh the rendered project description on PyPI; no
  runtime changes since 0.4.0.

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
