# Changelog

## 0.8.11 (2026-08-17)

### Added

- Codex Desktop/IDE threads whose exact active rollout is held open by their
  identity's `codex app-server` now render as `@ 托管`. This state is explicitly
  read-only: it carries no session pid and cannot be resumed, forked, stopped,
  deleted, or copied as a resume command. Every Codex execution and command
  copy refreshes the fd evidence first so a stale dead row cannot become a
  duplicate resume.
- Phone-friendly switching for csctl-managed tmux sessions: when the effective
  per-session `prefix2` is unset, csctl scopes it to `Ctrl-A`. `Ctrl-A s` then
  opens tmux's existing session tree from inside Claude, Codex, or Kimi without
  changing the primary prefix, global configuration, or an existing `prefix2`.

### Changed

- Re-verified Claude Code 2.1.233's foreground/background registry shapes and
  parent/child/unrelated trust semantics in a disposable authenticated fixture;
  re-verified the isolated release probe and focused compatibility fixtures.
- Re-verified Codex 0.147.0 project trust against official source and Kimi
  0.36.1 workspace trust against its shipped source: Codex exact-matches cwd,
  configured project root, and Git root; Kimi revoke deletes its positive
  trust record and leaves no negative footprint.
- Corrected the Sessions help and provider documentation to reflect Kimi's hook
  registry as a liveness source; evidence-less bare processes remain unbound.

## 0.8.10 (2026-08-16)

### Added

- **The kimi hook endpoint now registers on `SessionHeartbeat` too — a
  missed `SessionStart` self-heals within 60 seconds.** kimi delivers hook
  events fire-and-forget and swallows all failures, and 2026-08-16 showed
  the second real miss: a csctl-dispatched new session on kimi 0.36.1 ran
  14 turns over two hours with no registry entry and no `hook-errors.log`
  line, while the same launch path registered fine minutes before and
  after. With a `SessionHeartbeat` rule added to `~/.kimi-code/config.toml`
  (see README), kimi re-fires every 60 s while a session lives (the timer
  only runs when the event is configured), and the endpoint re-registers
  the session through the same verified write path — a missed start becomes
  a ≤60 s unbound window instead of an unbound lifetime. Two boundaries
  hold: hooks are startup config, so a session launched before the rule was
  added stays unbound until reopened; and a pre-first-prompt new session
  still has no sid at all, so nothing can bind it yet (the heartbeat does
  not fire sessionless, so it adds no error-log noise). On a csctl older
  than this release a configured heartbeat registers nothing and appends
  one `unknown-event` line per minute to `hook-errors.log` — upgrade csctl
  and add the rule together.
- Kimi hook contract re-verified on 0.36.1 (2026-08-16): SessionStart at
  first prompt for a new TUI, clean `/exit` now removes the entry via
  SessionEnd (0.35.0 did not), and `SessionHeartbeat` re-registration
  observed live (entry rewritten 62 s after the start registration).

## 0.8.9 (2026-08-13)

### Added

- **`csctl _kimi-hook` now leaves a trail when it does not register a
  session.** Every non-registering outcome appends one bounded line to
  `~/.kimi-code/run/hook-errors.log` (newest 50 kept) — malformed payload,
  unreadable ancestry, failed write, and notably an unrecognized
  `hook_event_name`, which is how a future kimi payload rename would first
  surface. Until now such a miss was invisible from all three sides: kimi
  swallows hook failures, nothing reads the endpoint's exit code, and the
  registry cannot record an event it was never handed — a live 0.35.0
  session sat unbound for four hours on 2026-08-13 with no evidence left to
  inspect. The trail is diagnostic only: it mints no binding, changes no
  exit code, and an unwritable log never alters the hook's outcome.

### Changed

- **Stale runtime-registry entries are pruned on each successful
  registration.** kimi's `SessionEnd` does not dependably fire (0.35.0 kept
  `run/<pid>.json` after a clean `kimi -p` exit and after a killed TUI), so
  the directory accumulated entries for dead pids. `prune_gone_entries`
  removes only entries whose pid is provably `GONE`; `UNAVAILABLE` and
  malformed entries are left alone, so a platform that cannot judge
  liveness never empties a registry it cannot read. Leftovers were already
  inert — the reader re-verifies identity and starttime — this bounds the
  directory.
- Kimi upstream contracts re-verified on 0.35.0 (2026-08-13) and the
  records corrected where measurement disagreed: the snake_case payload
  contract holds (0.35.0 builds hook input camelCase and converts before
  spawning), `SessionStart` still fires on both probed paths, and the
  "SessionEnd fires on a clean exit" claim in ADR-0005, `kimi.py`,
  `kimi_hook.py`, CONTEXT.md, and the version-stamped ledger in
  `docs/claude-code-compatibility.md` is now recorded as refuted.
- The `<pid>.json` name contract has one owner again: `kimi.py` exposes
  `prune_gone_entries` and `_entry_pid`, so the hook endpoint no longer
  re-derives the registry layout, and deletion of external state stays in
  `data/` where the architecture puts it.
- README documents what an unbound kimi session means for operators and
  points at the trail.
- `docs/releasing.md` and CLAUDE.md carry the post-publish cache-busting
  recipe that actually works (`uv tool upgrade --reinstall --no-cache`);
  `uv tool upgrade` has no `--refresh` flag and `--reinstall` alone was not
  enough for 0.8.8.

## 0.8.8 (2026-08-13)

### Removed

- **Remote Control management is gone.** The Projects tab loses its
  状态/自动远控/启动模式 columns, the `o`/`s`/`c`/`S` server verbs, and the
  RC-server listing, together with the whole RC data/action layer
  (`data/rc.py`, the RC outcomes, `claude remote-control` process
  discovery, and the `rc` tmux session it spawned into — `CSCTL_RC_SESSION`
  is retired with it). The tab is now a pure launcher plus membership
  curation: 项目/目录 columns, Enter CLI chooser, `x`/`k` direct launches,
  and the `p`/`h`/`H` curation verbs. The 证据 provenance-badge column
  (钉/隐/信cc/信cx/信km/活…) is removed with it — provenance stays on the
  row model for ordering and status-bar counts. Session-level remote-control
  exposure (the 📱 badge on the Sessions tab) is unaffected.
- **Background agents management is gone.** The 后台 tab, the `csctl agents`
  command, and the `jobs/<short>/state.json` registry scan are removed;
  csctl no longer lists, takes over, respawns, watches, stops, or removes
  Claude Code background agents. Session cleanup still removes a dead
  session's `jobs/<sid-prefix>` artifacts exactly as before.

### Changed

- **The TUI title now reads "Agent CLI 会话管理器"** (was "Claude Code
  会话管理器") — the tool has managed Codex and Kimi Code sessions since
  ADR-0005, and the title finally says so. The package, command, and config
  directory names are unchanged.
- Projects-tab rows no longer gate on a missing `~/.claude.json` silently:
  an unreadable or malformed project map surfaces as a typed ⚠ 项目来源异常
  status count, the same channel as codex/kimi trust-store and curation
  degradation.

### Deprecated

- **The claude-session-doctor skill is deprecated.** Its background-agent
  instructions and RC-management verbs depended on surfaces removed in this
  release; session rescue stays in csctl itself (`csctl resume` and the
  TUI).

## 0.8.7 (2026-08-13)

### Added

- **Several codex identities on one machine (ADR-0008).** `~/.config/csctl/
  providers.json` may declare `codex_homes`; each declared state home becomes
  its own provider with its own sessions, trust records, provenance badges,
  CLI-column label, and launcher entry. The declaration is the complete codex
  inventory, so an inherited `CODEX_HOME` — which every codex session exports
  to whatever it spawns, csctl included — no longer decides what csctl can
  see: launched from inside a second identity's session, csctl previously
  showed that identity's sessions and none of the default home's. Commands
  csctl synthesizes for a declared identity now state `CODEX_HOME` explicitly
  (tmux `-e` on the spawned window, `os.environ` before `execvp`, a quoted
  leading assignment in copied commands, an env mapping for `codex delete`),
  so a copied `codex resume <sid>` no longer fails in a shell that inherited
  a different identity. Running processes are attributed to an identity via a
  whitelisted `/proc/<pid>/environ` read of `CODEX_HOME`; this refines the
  unbound-live hint only and never filters liveness, which was already
  identity-safe. Without the file, behavior is unchanged in every detail.

- **Projects-tab membership is now evidence-tiered and multi-CLI
  (ADR-0007).** A project is an absolute directory plus a provenance
  evidence set: **Pinned** (operator-curated, immune to hygiene and decay),
  **Trusted** (Claude effective trust as before, now joined by codex
  `config.toml [projects.*] trust_level="trusted"` keys and kimi
  `workspace-trust/<id>` roots — both exact-match only), and **Observed**
  (any provider's session activity in the directory; observed-only entries
  decay out of the tab after 30 days). Trust inheritance still only
  qualifies recorded candidates and never generates them, so a trusted `/`
  cannot flood the tab. Rows show their provenance as 钉/隐/信cc/信cx/信km/活…
  badges; ordering is pinned-first, then activity. Migration is purely
  additive — every previously listed directory still qualifies.
- **New `p`/`h`/`H` curation verbs on the Projects tab**, backed by csctl's
  own curation store (`~/.config/csctl/projects.json`, XDG-respecting):
  `p` pins/unpins a directory, `h` hides it (suppresses every evidence
  tier), `H` toggles listing hidden rows so they can be unhidden. The store
  is advisory-locked, atomically replaced, and preserves foreign keys; the
  CLI trust stores stay read-only. Membership-source degradation (codex/kimi
  trust, curation) surfaces as a typed ⚠ 项目来源异常 status count, never a
  blanked tab.

## 0.8.6 (2026-08-12)

### Added

- **Opt-in kimi runtime registry closes the unbound-session gap for good.**
  Two `[[hooks]]` rules in `~/.kimi-code/config.toml` (README) point kimi's
  official SessionStart/SessionEnd hooks at `csctl _kimi-hook`, which
  maintains `~/.kimi-code/run/<pid>.json` (`sessionId` + `procStart`) — the
  CLI's own self-report, the same evidence shape as Claude's
  `sessions/<pid>.json`. Every kimi session binds once it materializes (a
  TUI's first prompt): bare-launched TUIs included, not just csctl-dispatched
  windows. Entries are re-verified against the /proc walk (identity +
  starttime), so stale, forged, or double-attach-disputed records never bind.
  Hook payload/timing contract verified on Kimi Code 0.34.0.
- Codex Desktop sessions now get their own `桌面` source badge: Desktop main
  threads reuse the IDE pipeline (`source: "vscode"`), so only the exact
  `originator: "Codex Desktop"` distinguishes them from real VS Code sessions
  (real rollouts sampled, cli_version 0.130.0–0.147.0).

### Removed

- The 0.8.5 `_bind-window` late-sid backfill watch — subsumed by the runtime
  registry, which covers its entire case (dispatched new sessions) with
  stronger evidence and no per-window polling process. Without the hook
  configured, new-session dispatches return to the pre-0.8.5 unbound-hint
  behavior until a later resume declares the sid.

## 0.8.5 (2026-08-12)

### Fixed

- **Kimi sessions dispatched as NEW sessions no longer read as `? 未知` after
  their first prompt.** Kimi registers a session (index entry + sid) only at
  the first prompt and refuses `--session <unknown-id>`, so the dispatch
  window could not declare `@csctl_sid` at spawn and the metadata binding
  (0.8.3) never engaged — csctl's own dispatches looked like unbound bare
  TUIs. Late-sid providers (`caps.late_sid`, kimi) now embed a background
  `csctl _bind-window` watch in the spawn command, which backfills
  `@csctl_sid` from a spawn-time index snapshot diff once exactly one new
  session registers for the dispatch directory, re-verifying the pane process
  identity first; ambiguity or evidence loss fails closed (unbound), never a
  wrong binding. Kimi upstream contracts re-verified on 0.34.0 (title rewrite
  now collapses to bare `kimi`; wire-log fd is transient; no env evidence).

## 0.8.4 (2026-08-08)

### Changed

- **All csctl-dispatched agent sessions now share one `csctl` tmux session.**
  New, resumed, forked, backgrounded, and background-agent respawn windows use
  project-prefixed names, making cross-project switching available from one
  tmux window list. Existing resident windows are entered in place and are not
  migrated; managed Remote Control servers remain in the separate `rc` session.
  Session/window names are exact-matched, and RC configuration rejects tmux
  target-expression syntax so prefix or glob fallback cannot cross that boundary.

### Fixed

- Background-agent respawns now enter the job's recorded project directory
  before launching inside the shared `csctl` session and fail closed when that
  directory is missing or unusable.
- Concurrent first dispatches recover when another csctl process wins creation
  of the shared tmux session, then create their window exactly once.

## 0.8.3 (2026-08-04)

Operator feedback fixes: kimi liveness restored via tmux window metadata, and
a CLI chooser on the Projects launcher.

### Fixed

- **Kimi sessions dispatched by csctl no longer read as dead.** Kimi 0.31.1
  rewrites its own process title at runtime (observed live: `/proc/cmdline`
  becomes `kimi-code` plus padding — the `--session <sid>` argv csctl itself
  dispatched is destroyed), which blinded argv-exact liveness entirely. csctl
  now stamps `@csctl_sid` / `@csctl_provider` window options on every
  tmux window it spawns (addressed by server-unique `#{window_id}`) and binds
  the pane process back to its session through that metadata — with exe/comm
  identity verification, dead-pane filtering, bidirectional pid↔sid
  uniqueness, fresh `proc_start` capture, and the same execution-time
  re-resolution before any signal is sent. Bound kimi sessions regain
  live status, `⧉` residency, in-place attach, and `s` stop. The unbound-live
  `[live?]` hint now recognizes kimi processes by comm/exe identity too, so
  bare title-rewritten TUIs surface again. Upstream contract recorded in
  `docs/claude-code-compatibility.md`; ADR-0005 amended (kimi liveness grade
  is now tmux-metadata for dispatched windows; bare TUIs stay blind).

### Changed

- **Projects `Enter` now opens a CLI chooser** (user-requested): pick
  claude / codex / kimi with arrows + Enter (Esc cancels); the first row is
  claude, so Enter-Enter reproduces the old behavior. `x` / `k` remain as
  direct shortcuts. Friendlier on mobile SSH clients where letter shortcuts
  are awkward.

## 0.8.2 (2026-08-04)

Closes out the codex/kimi coverage-gap audit (batches A+B): discovery depth,
honest liveness display, and official-verb delegation for non-Claude sessions.

### Added

- **Codex label body fallback**: rollouts without a `session_index` thread
  name take their label from the first user message in the rollout body
  (bounded read: 64 lines / 128 KB) instead of showing `(untitled)` — on the
  audited machine that was 250 of 252 rows.
- **Codex archived sessions discovered**: `~/.codex/archived_sessions/` rows
  are listed with an archived marker; resume-family verbs refuse and hand
  back the official `codex unarchive <sid>` (headless rows are tagged
  `(archived)`; `y` copies the unarchive command).
- **Codex delete delegation**: `d` on a dead, non-archived codex row runs the
  official `codex delete <sid>` (bounded subprocess, typed result, fresh
  execution-time guards). csctl's own removal seam still never touches
  non-Claude state; kimi keeps refusing (no official delete verb upstream).
- **Codex name-resume binding**: processes started as
  `codex resume <thread-name>` bind via a unique session_index name→id
  reverse lookup; ambiguous or unknown names bind nothing (fail closed).
- **Fourth liveness state for non-Claude rows**: when an unbound live
  same-CLI process shares a session's directory, the newest candidate row
  shows `? 未知` and Enter/t/R ask a no-kill confirm about the double-attach
  risk (headless: `[live?]`). Bound live non-Claude rows now read `● 活`
  (busy/idle is unknowable upstream) instead of a false `● 闲`.
- **Codex source badges**: vscode / ChatGPT-Android-remote originators render
  as IDE / 远程 badges instead of a misleading CLI badge (sdk hiding for
  `codex_exec` is unchanged).

### Fixed

- A deleted kimi session directory no longer turns every
  `csctl resume <keyword>` into a refusal: a missing body file is a
  no-match, while other read errors still refuse loudly.
- Kimi body search now reads the real conversation
  (`agents/main/wire.jsonl`) instead of `state.json` metadata, so keywords
  that only appear in the dialogue finally match kimi sessions.
- Codex kill-target safety: an unquoted `codex exec … resume … <uuid>`
  prompt no longer binds the exec process to that session (UUIDs bind only
  in the resume-target position, `exec` before `resume` never binds).
- Codex first-line read cap raised 64→256 KB, with an honest over-cap issue
  message instead of a misleading "upstream format change?" hint.
- Honest non-Claude wording everywhere it lied: headless live rows no longer
  claim Claude's guarded-takeover semantics; `y` notes the copied command
  does not stop the running process; `--take-over` on a non-Claude sid names
  the owning CLI (archived sids get the unarchive command) and its help is
  marked Claude-only; help screen, README, CONTEXT, and ADR-0005 no longer
  claim launcher-created sessions are matchable.

## 0.8.1 (2026-08-04)

### Added

- **Multi-CLI workbench (ADR-0005)**: csctl now manages Codex CLI and Kimi
  Code sessions alongside Claude Code. A new `data/providers/` layer owns
  per-CLI adapters (typed capabilities, argv synthesis, disk discovery):
  - Sessions tab lists all providers with a CLI column (`cc`/`cx`/`km`),
    per-CLI counts, provider filter terms, and non-fatal source-degradation
    display; `f` fork is capability-gated (codex: native `codex fork`;
    kimi: refused — in-session `/fork` only).
  - Projects tab launcher goes multi-CLI: `Enter` = new claude, `x` = new
    codex, `k` = new kimi session in the project's tmux window.
  - Headless `csctl resume` lists all providers with ready-to-copy native
    resume commands (`codex resume <sid>` / `kimi --session <sid>`);
    non-Claude rows are tagged.
  - Non-Claude liveness is argv-exact: a process binds to a session only
    when its argv carries the session id (how csctl itself dispatches into
    tmux). Bare TUIs and codex daemons are never stop/takeover targets;
    live non-Claude takeover re-resolves fresh evidence at execution time.
  - `CSCTL_PROVIDERS` restricts the provider allowlist; `CODEX_HOME` /
    `KIMI_CODE_HOME` (official variables) relocate discovery.
  - Cleanup remains Claude-only by design; Agents and Projects-RC surfaces
    remain Claude-specific.

## 0.8.0 (2026-07-30)

### Removed (BREAKING)

- **Bridge-environment ledger pipeline dropped**: `csctl env`,
  `environments.py`, `environment_ledger.py`, `rc_environment.py`, and the
  on-disk ledger (`$XDG_CONFIG_HOME/csctl/environments.jsonl`) are gone.
  csctl never had a way to deregister a cloud environment, so the ledger's
  orphan list could never become actionable and was already incomplete by
  its own admission (environments minted while csctl was not running were
  never recorded). `Bridge Environment` (the `session_*`/`cse_*` namespaces,
  `is_rc_exposed`) stays a modeled concept — csctl just keeps no history of
  it.
- **Headless CLI narrowed to `resume` + `agents`**: `csctl prune`,
  `csctl env`, `csctl skill install/uninstall`, and the whole `csctl rc *`
  subtree are removed. Each duplicated a capability the operator already
  has in the TUI — cleanup (Sessions `c` submenu), RC start/stop (Projects
  `o`/`s`/`c`), and the bundled skill (now distributed via the skills CLI)
  — leaving the headless surface to its one real consumer, the
  claude-session-doctor skill.
- **Autostart list retired**: the `rc-enabled` list, `csctl rc up`, the
  Projects `a`/`A` keys, and the 开机自启 column/status count are gone —
  starting Remote Control is on-demand only (`o`). `cfg.rc_stagger`
  (`CSCTL_RC_STAGGER`) and `cfg.config_dir`/`XDG_CONFIG_HOME` are removed
  along with their last consumer.
- **OSC 11 terminal-theme auto-detection dropped**: `detect_mode()` is now
  `cfg.theme`/`CSCTL_THEME`/`--theme` → `$COLORFGBG` → dark. Under
  tmux-first, the query's only beneficiary (a bare first launch) rarely got
  an answer anyway, so every startup no longer pays for a probe that mostly
  produced no verdict.

See `docs/adr/0004-surface-reduction-to-the-operator-core.md` for the full
rationale and the usage evidence behind each removal.

### Changed

- **Removal hardening downgraded to lstat identity revalidation**: the
  `renameat2(RENAME_NOREPLACE)` claim/rollback and the fd-chain
  `O_NOFOLLOW` root walk are gone, per a threat-model ruling that "a local
  attacker swaps paths between preview and execute" is out of scope for a
  single-operator panel over its own `~/.claude`. Three protection layers
  remain: business revalidation on fresh evidence (delete ⊆ preview), root
  containment, and lstat identity (device/inode/file type) frozen with the
  plan and re-checked at execution — any mismatch, including a target that
  became a symlink, is refused.
- **Meta-test and dead-surface cleanup**: CI-shape snapshot tests and a
  residual CLI test module were merged/dropped, several
  structurally-duplicated adapters collapsed (`AgeCleanupPlan`,
  `cleanup_selection`, `ExecutionSessionState`), and a third tier of
  production-dead compatibility wrappers (zero src callers) removed across
  `environments`, `agent_ops`, `session_ops`, `rc`, `proc`, and
  `WorldSnapshot`.

### Upgrading from 0.7.x

- `csctl prune ...` → TUI Sessions tab, `c` cleanup submenu (same five
  cleanup classes, preview-first).
- `csctl rc add/rm/up/stop/status/list` → Remote Control is purely
  on-demand now: TUI Projects tab `o` starts, `s` stops, `c` toggles the
  per-project `remoteControlAtStartup` flag. The autostart list is retired;
  a leftover `$XDG_CONFIG_HOME/csctl/rc-enabled` (default
  `~/.config/csctl/rc-enabled`) is inert and safe to delete.
- `csctl env` → removed; cloud environments are managed on
  claude.ai/code (csctl never had deregister capability). A leftover
  ledger at `~/.config/csctl/environments.jsonl` is safe to delete.
- `csctl skill install/uninstall` → install via the skills CLI instead:
  `skills add dzshzx/agent-skills --skill=claude-session-doctor`. Delete
  the old `~/.claude/skills/claude-session-doctor` copy before
  reinstalling.
- Light terminal themes → auto-detection is gone; set `CSCTL_THEME=light`
  or pass `--theme light`.

## 0.7.6 (2026-07-30)

### Changed

- **Slimming pass, no behavior change on success paths**: the seven
  structurally identical per-domain issue dataclasses collapse into one
  canonical `InventoryIssue` (plus one shared detail renderer), and the
  three hand-rolled atomic-write pipelines (project settings, environment
  ledger, rc-enabled list) now share a single `data/atomic_write.py`
  mechanism while keeping their typed stage/result surfaces. One deliberate
  widening: an unencodable-content `UnicodeError` during settings/ledger
  writes now surfaces as a typed stage failure instead of an unhandled
  crash (matching the rc-enabled store's existing contract). The remaining
  compatibility stragglers (`take_over` string view, `rc.scan_servers`)
  are gone; drifted test factories consolidated.

## 0.7.5 (2026-07-29)

### Changed

- **Hardening batch**: destructive paths now run on typed, fail-closed
  evidence end to end — cleanup plans/previews and executors share one
  protection authority (including pathname-only transcript sids), refresh
  publishes immutable single-generation batches, resume/takeover re-resolves
  the live session at execution time instead of trusting keypress-time PIDs,
  `rc-enabled` updates are locked read-modify-write transactions with exact
  persistence-stage results, and RC pane capture is bounded (2000 lines /
  1 MiB) with negative-cache backoff. Copied live-resume commands emit
  `csctl resume --take-over <sid>` instead of a raw `kill <pid>`.
- **Cleanup safety**: orphan removal atomically claims its target with
  `renameat2(RENAME_NOREPLACE)` and verifies identity, so a same-name
  replacement created between preview and apply can no longer be deleted;
  platforms without the syscall get a typed refusal (including a libc
  self-handle failure, which previously crashed at import).
- **Quality gates**: CI runs a 3.12–3.14 matrix with Ruff, mypy, coverage
  floors ratcheted to measurement (91% statements / 82% branches), and a
  file-size gate that now also enforces a 1000-line hard cap on test
  modules. Production publishing requires an annotated, version-matching
  tag plus the full quality gate.
- **Dead weight removed**: the bool/list/text compatibility shims left by
  the typed-outcome migration (and the write-only `ActionStatus` taxonomy)
  are gone; every caller consumes the typed results directly.
- `CSCTL_CLEANUP_AGE_DAYS=0` is accepted again (sweep every aged entry);
  invalid values still fail fast with a typed message.

## 0.7.4 (2026-07-23)

### Changed

- **Platform temp directories are excluded from 项目 membership**: paths at or
  beneath `tempfile.gettempdir()`, `/tmp`, or `/var/tmp` no longer surface in
  the 项目 tab / `csctl rc status` via trust discovery alone. This is a
  membership rule, not a trust rule — `~/.claude.json` is untouched, so a
  deliberately trusted `/tmp` keeps suppressing dialogs for scratch sessions
  while its basename no longer shows up as a launchable "project". Explicitly
  actionable entries (in the autostart list, or holding an rc tmux window)
  stay listed, mirroring the existing missing-dir residue escape. Single
  predicate: `rc._is_temp_path` (segment-boundary matching, normpath only).

## 0.7.3 (2026-07-23)

### Added

- **Activity ordering for the 项目 tab and `csctl rc status`**: projects sort
  by their most recent session activity (exact-cwd join against the shared
  world snapshot; the CLI runs one transcript scan, like `csctl resume`), and
  never-active projects sink path-ascending — broad-root members such as a
  trusted `/tmp` no longer crowd the launcher's top. The cursor follows the
  focused project's identity (`row_key`) across refresh reorders instead of
  sticking to a list position; the no-snapshot fallback keeps plain path
  order. Single ordering source: `rc.order_by_activity`.

### Changed

- **项目 membership is path-keyed with effective trust — the workspace-root
  concept is gone**: the 项目 tab (and `csctl rc status`) now lists every
  `~/.claude.json` project that is *effectively trusted* — its own entry or
  any ancestor entry has `hasTrustDialogAccepted: true` — and whose directory
  exists, wherever it lives on disk. No more single `~/workspace` assumption,
  so projects under any layout (e.g. `~/workspace-external/*`) appear. This
  also fixes the inverted subdirectory case: claude suppresses the trust
  dialog under a trusted ancestor and records an explicit-False flag (verified
  on claude 2.1.218 — declining the dialog writes no entry at all), so a real
  project created under a trusted parent now shows up instead of being
  filtered out. The single predicate is `models.effective_trust` (ancestor
  matching by path-segment boundary; normpath only, never realpath).
- **Absolute path is the primary key everywhere**: `RCProject.directory` keys
  every join; `RCProject.name` is a derived basename for display only.
  `rc-enabled` stores absolute paths — legacy short-name lines are migrated in
  place on first read (atomic tmp+rename rewrite, comment lines preserved)
  using a frozen copy of the old workspace detection, including one final
  `CSCTL_WORKSPACE` read. `csctl rc add/rm/stop` take directory paths
  (`csctl rc add .` still works); `rc status`/`rc list` print paths.
- **Managed RC windows are joined by path metadata, not by name**: `start_one`
  declares `@csctl_path` on the window (with `pane_current_path` as adoption
  fallback for pre-0.7.3 windows) and kill/capture address the server-unique
  `#{window_id}` — window names are cosmetic, so basename collisions can no
  longer hit the wrong window via tmux's prefix matching. The RC server
  `--name` is now the project basename (was `ws/<short-name>`); existing
  running servers keep their old `ws/` display name until restarted.

### Removed

- `--workspace` CLI flag, `CSCTL_WORKSPACE` environment variable (still read
  once during rc-enabled migration), and `config._detect_workspace`.

## 0.7.2 (2026-07-11)

### Added

- **Adaptive terminal theme**: the TUI detects the terminal background at
  startup (`CSCTL_THEME`/`--theme` override → OSC 11 query → `$COLORFGBG` →
  dark) and registers a dark or light palette accordingly (new top-level
  `theme.py`; both palettes are generated from ONE spec so the semantic attr
  set can't diverge). List/body attrs now use the terminal's default
  background instead of forcing near-black, so csctl blends into the
  terminal's own theme; light terminals get dark-on-light foregrounds kept
  ≥ 4.5:1. The OSC 11 query ships with a DA1 sentinel, so terminals that
  ignore it (tmux without an explicit bg — the common csctl case, measured
  0.3ms) answer in one round-trip instead of stalling startup for the full
  reply timeout; inside tmux `auto` therefore falls back to dark — force
  `light` explicitly on a light terminal.

## 0.7.1 (2026-07-07)

### Changed

- **项目 tab no longer lists pure trust residue**: a project whose workspace
  directory is deleted and that is neither in the autostart list nor holding a
  tmux window is now dropped by `rc.scan()` instead of rendered as a ✖ 缺失
  row — csctl can't act on it (no start, and it never edits `~/.claude.json`).
  Missing-dir projects that ARE actionable (stale rc-enabled entry, or a
  server still running out of the deleted dir) stay listed. Applies to both
  the TUI and `csctl rc status` (single source in `scan()`).

## 0.7.0 (2026-07-06)

**tmux-first dispatch** (ADR-0001, `docs/adr/0001-tmux-first-session-dispatch.md`):
csctl is repositioned from a session panel into a tmux-first dispatch center —
every primary verb now puts the operator into (or the session into) a
per-project tmux window, so sessions survive terminal/SSH disconnects by
default. **Muscle memory from ≤0.6.x breaks deliberately**: `Enter` now lands
in tmux instead of the bare terminal.

### Changed

- **Sessions tab `Enter` = tmux 接回** (was: bare-terminal resume). A
  tmux-resident session is entered in place (no kill, no confirm); anything
  else resumes into its per-project tmux window and enters, with the usual
  takeover confirm / R10 gate. Bare-terminal resume moved to **`t` 终端接回**
  (the fallback; on a resident session it pulls the session out of tmux via
  the standard takeover confirm).
- **Sessions tab `R` = 转后台 without Remote Control**: moves a bare session
  into its per-project tmux window, does not enter it, stays in csctl. A
  resident session is refused with 已在 tmux.
- **`f` 分叉 now forks into tmux** (own `<sid8>-fork` window) and enters it.
- **Projects tab `Enter` = 新建 tmux 会话并进入** (was on `t`); the RC-server
  start moved to **`o` 启动远控**. `t` is unbound on this tab.
- **后台 tab `Enter` = tmux 接回** (resident worker entered in place); new
  **`t` 终端接回**; the old `o` alias is dropped (o now means RC start on the
  Projects tab only).
- **Tab order is launcher-first: 项目 → 会话 → 后台**, startup lands on 项目.

### Removed

- The session-level RC relaunch (`R` with `--remote-control`; internals
  `relaunch_in_tmux` / `tmux_resume_cmd` / `_rc_name`). The Sessions tab can
  no longer mint cloud environments at all — every fresh `--remote-control`
  process minted a new cloud env entry with no local deregister. Phone/web
  control stays available via the Projects tab (`o` / `c`) and the in-session
  `/remote-control`.

### Added

- **tmux-residency badge ⧉** in the Sessions 状态 column for live sessions
  running inside a tmux pane (U+29C9, width-stable). Residency is
  batch-computed once per refresh (`tmux.residency_targets`: one
  `list-panes -a` + `/proc` ancestor chains) into `Session.tmux_target`; the
  badge and the resume/backgrounding actions read the same field.

## 0.6.5 (2026-07-06)

Architecture-review refactor batch #2 (4 deepening candidates, re-verified
against source before landing) + the two-axis code review's judgement-call
cleanup. No behavior change except the one deliberate item listed at the end.

- **One liveness-inputs assembly.** `liveness_inputs()` (the
  `(session_procs, cur, agent_jobs, agents_map)` fetch) moved from
  `data/snapshot.py` down into `data/liveness.py`, where cleanup can import
  it: the two hand-kept mirrors inside `cleanup` (`_gather_known`'s
  per-source self-fetch and `execute_session_removals`' inline fetch, plus
  the "keep the two in sync" comment) are gone. A shared
  `_fill_liveness_inputs` helper owns the fill-the-gaps ladder.
- **`data/tmux.py` — the tmux adapter is its own module.** The generic tmux
  seam (only `_tmux_run` touches `subprocess`; swallow-errors verb wrappers,
  `run_in_tmux`, `session_name_for`, `find_session_window`, enter/attach
  verbs) moved out of `data/rc.py`; `rc.py` keeps RC-server domain logic plus
  four thin `cfg.rc_session`-scoped delegates. `actions/session_ops` and
  `actions/agent_ops` no longer import `rc` at all — the session resume paths
  are decoupled from Remote Control, matching CONTEXT.md's warning not to
  conflate the two.
- **Cleanup's interface closed.** New public
  `cleanup.remove_agent_artifacts(short, sid)` (caller owns the alive/R10
  gates); `agent_ops.remove_job` no longer pokes `cleanup._remove_path` /
  `cleanup._session_artifact_paths`. No production code outside
  `data/cleanup.py` touches a cleanup private anymore.
- **One cleanup count vocabulary.** `csctl prune` now builds the same frozen
  `CleanupPlan` the TUI uses and derives its summary from `plan.counts()`;
  `--sweep-orphans` reuses the plan's frozen orphan list. The raw-tally
  `cleanup_stats` (whose docstring still claimed a view contract the view had
  left) and the consumerless pass-through `cleanup_classified` are deleted.

Deliberate user-visible change (the only one):

- The `csctl prune` summary header now reports "actionable now" counts from
  the plan — `Total: N  Prunable empty: X  short(<=2): Y  Orphan dirs: Z
  Zombie files: W  Aged: V` — instead of raw tallies that contradicted the
  adjacent "Would prune N session(s)" line.

## 0.6.4 (2026-07-06)

Architecture-review refactor batch (8 deepening candidates, adversarially
reviewed before landing) + a two-axis code review with its fixes. No intended
behavior change except the deliberate items listed at the end.

- **Sessions filter mode owns its keys (bug fix).** The filter Edit now lives
  in the view's own frame footer, and `App._input` asks `captures_text()`
  before consuming global keys. Fixes two real defects: typing a keyword
  containing `q` quit csctl outright, and a notify restore-alarm left over
  from ≤3s before entering the filter evicted the (still key-eating) Edit.
  `deactivate()`/`own_footer()`/`release_footer()` are gone from the TabView
  Protocol and the App façade.
- **One plain-stop confirm policy.** `views/_confirm.py::confirm_stop` is the
  symmetric twin of `confirm_takeover`; the three views' hand-written stop
  triads collapse to one call each (`gated=False` expresses the RC tab's
  tmux-window stop, which never needed the R10 gate).
- **One kill primitive.** `session_ops.take_over(pid, proc_start)` owns
  R10 gate → kill-time `pid_alive` recheck (closes the pid-reuse window while
  a confirm modal sits open; `Session`/`resume_takeover` now carry
  `proc_start`) → SIGTERM → settle → cache invalidation, returning
  `killed/gone/refused/failed`. The five hand-copied (and already diverging)
  kill sequences consume it; `relaunch_in_tmux`/`do_tmux_resume` fold into one
  `_spawn_in_tmux` skeleton.
- **Cleanup runs off ONE frozen plan.** `cleanup.build_plan` →
  `CleanupPlan`: the status-bar counts, the preview overlay, and the CLI
  dry-run all read the same candidates, and the new `execute_*` functions
  delete AT MOST that list, revalidating each item against fresh protection
  data (删除 ⊆ 预览 — including the transcript tier, fed by a fresh scan from
  the caller). Each TUI submenu action is one table-driven `_CleanupAction`
  record; the old `remove_orphan_dirs`/`remove_zombie_session_files`/
  `remove_aged_entries` are gone.
- **One agent host-enrich loop.** `liveness.enrich_jobs(jobs, session_procs)`
  replaces the three copies (snapshot / agents view / `csctl agents`); the
  per-job full-registry `/proc` re-injection is gone.
- **`proc_alive` is a tri-state sentinel.** Raw registry rows carry `None`
  (= not injected); `select_zombie_pids` and `host_pid_for_sid` refuse it, so
  misusing raw rows fails safe (deletes nothing) instead of classifying every
  session file as a zombie.
- **One degraded-fetch assembly.** `snapshot.liveness_inputs()` feeds both
  `build_world_snapshot` and the Sessions view's `fetch_pending(None)`
  self-fetch (the documented "mirrors the snapshot" copy is gone).
- **One ledger pipeline.** `environments.reconcile(...)` →
  `models.Reconciliation` owns the R6 order (observe → upsert → observe_live →
  classify, orphans against the FILE-REFERENCED tier); snapshot and
  `csctl env` both consume it. `manual_delete_list` (dead product code after
  the switch) is removed — `csctl env` prints the same checklist from
  `recon.orphans`.

Deliberate visible changes: Tab is captured (inert) while the filter Edit is
open — Enter/Esc first; the footer shows only the filter hints while typing
(the Tab/q/r prefix promises would be false); the RC not-running notice reads
"远控服务未在运行" (was bare "未在运行"); the 空壳/短 counts in the status bar
and cleanup submenu now mean "prunable now" (excluding alive/current/recent),
matching the preview exactly; `csctl prune --apply` prints the revalidated
removed count; an already-gone/recycled pid is no longer SIGTERMed and skips
the 1s settle on every path.

## 0.6.3 (2026-07-06)

Post-review cleanup batch on top of 0.6.2's refactor series; no behavior
change.

- **Default `handle_key` lives in `ListTabView`.** Overlay mode is answered by
  the new `_overlay_active()` hook, list mode dispatches from `KEY_TABLE`;
  Agents/Projects drop their hand-written copies and Sessions handles only its
  extra modes (filter/cleanup/preview) before falling through to the base.
- **Tighter types on the exit seam.** `App.exit_with`/`result`/`run` are typed
  as `ExitIntent` (TYPE_CHECKING import) and `KEY_TABLE` as `tuple[Key, ...]`.
- **`Key.help` → `Key.help_lines`** — no builtin shadowing; the name states the
  lines are pre-indented display lines.
- Doc fixes: `split_env_id`'s docstring no longer claims a nonexistent
  `EnvRecord.env_id`; the environments section header reflects the liveness
  routing.

## 0.6.2 (2026-07-06)

Architecture-review refactor batch: seven deepening refactors, no intended
behavior change (two deliberate cosmetic alignments noted below).

- **One liveness assembly point.** `liveness.live_session_procs()` now owns the
  `proc_alive` injection that was inlined at 7 call sites (forgetting it made
  everything read as dead, silently); all consumers go through the one seam.
- **One takeover-confirm policy.** `views/_confirm.py::confirm_takeover` owns
  the R10-degrade-gate → `would_take_over` → confirm sequence and the confirm
  文案 templates (previously copy-pasted at 4/7 sites); `_DEGRADED` has one
  definition.
- **Shared tab base class.** `views/_base.py::ListTabView` hoists the
  walker/listbox/status frame, focus-preserving rebuild, centered overlay,
  footer guard, and overlay-mode key dispatch that all three tabs re-implemented.
  Views talk to App only through an explicit façade (`is_active`/`own_footer`/
  `release_footer`) — no more `app.frame`/`app._active` pokes.
- **Single-source key tables.** `views/_keytable.py`: each tab declares one
  `KEY_TABLE` and its footer hints, help overlay, and key dispatch are all
  generated from it (the `_colspec` move applied to keys). Sessions/Projects
  footer + help output is byte-identical; the Agents help is reformatted to the
  same per-key layout as the other tabs (content preserved).
- **Single-source Bridge Environment observation.** `models.split_env_id` is
  the one namespaced-id parser (replacing four divergent implementations);
  `liveness.is_rc_exposed` is public and `environments.observe_live` actually
  calls it; `observe`/`observe_live` converge into one alive-gated collector.
  The RC stop-confirm now truncates very long project names like the other
  tabs' confirms (cosmetic alignment).
- **`ExitIntent` replaces the exit-tuple ladder.** Resume-family actions exit
  the MainLoop as self-finalizing intent dataclasses; `App` keeps one generic
  `exit_with(intent)` and `cli._cmd_tui` one `intent.run()` — adding a resume
  variant no longer touches `app.py`/`cli.py`.
- **Deleted the `data/agents.py` re-export shim** (its stated reason was stale;
  product code already imported from `liveness` directly).

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
