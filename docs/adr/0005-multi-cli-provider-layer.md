# Multi-CLI provider layer

Status: accepted (2026-08-04)

csctl was built as an operator panel for **Claude Code alone**: every scan
reads `~/.claude`, every resume synthesizes a `claude --resume` argv, and the
domain language (CONTEXT.md) says "Claude Code session" where it means
"session". The machine it operates, however, runs three agent CLIs side by
side — Claude Code, Codex CLI, and Kimi Code — and the operator problem csctl
solves (which sessions exist, which are live, dispatch them into per-project
tmux windows so they survive disconnects) is identical for all three. This
ADR turns csctl into a multi-CLI workbench by introducing a **provider
layer** while keeping the tmux-first dispatch model of ADR-0001 as the shared
engine.

Comparable workbenches (claude-squad, vibe-kanban, crystal, agent-deck)
converge on the same split this ADR adopts: a per-CLI adapter that owns
command synthesis and session identity, over a CLI-neutral engine that owns
terminal/tmux/worktree mechanics — from claude-squad's minimal
`{name, program}` profiles to vibe-kanban's typed `CodingAgent` adapters with
capability bits. All of them are **spawn-managed** (they only see sessions
they launched, with liveness = their own process handle, and busy-state read
from pane-text regexes); csctl's distinguishing capability — disk-level
discovery of *all* sessions plus `/proc`-grade liveness — is kept, per
provider, to the extent each CLI's on-disk state allows. agent-deck's honest
"integration level" tiers (Full for one or two CLIs, shallower for the rest)
become typed per-provider capabilities here rather than a docs table.

## Verified upstream facts (this machine, 2026-08-04)

The provider designs below are grounded in probes of the actual CLIs
(re-verify per release in `docs/claude-code-compatibility.md` style):

- **Codex CLI 0.146.0**: sessions live in
  `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` (NDJSON; first
  line is a `session_meta` record carrying `payload.session_id` (thread id),
  `cwd`, `originator`, `thread_source`, `cli_version`); an append-only index
  `~/.codex/session_index.jsonl` maps `id` → `thread_name`/`updated_at`.
  Resume: `codex resume <SESSION_ID>` (UUID; also `--last`); fork:
  `codex fork <SESSION_ID>` (native subcommand, verified on 0.146.0).
  Old rollouts may be zstd-compressed (`.jsonl.zst`) — v1 discovery reads
  plain `.jsonl` only and skips `archived_sessions/`. Subagent rollouts
  carry `thread_source: subagent` and a `parent_thread_id`. An
  interactive `codex` TUI does **not** hold an open fd on its rollout file
  (probed after a submitted prompt), so fd-based pid↔sid matching is
  impossible; a `codex resume <sid>` process, however, carries the sid in
  its argv. The shared `codex app-server` daemon (Codex Desktop / IDE) holds
  open fds on *many* rollouts at once — killing it would kill every
  desktop-hosted session, so it must never be a takeover target. Daemons
  (`app-server`, `proxy`, `codex-threadripper`, Windows-side `/mnt/c/...`
  binaries) share the `codex` basename and must be excluded from session
  matching by argv shape.
- **Amendment (2026-08-04):** `archived_sessions/` is no longer skipped:
  discovery now walks that flat store too through the same first-line
  parse, marking its rows archived; resume-family verbs refuse archived
  rows and hand back the official `codex unarchive <sid>` instead.
  Plain-`.jsonl`-only reading is unchanged (`.jsonl.zst` stays unread).
  The bullet above is left as originally written.
- **Kimi Code 0.31.1**: `~/.kimi-code/session_index.jsonl` maps `sessionId`
  → `sessionDir`/`workDir`; each `sessions/wd_<name>_<hash>/session_<uuid>/`
  holds `state.json` (`title`, `lastPrompt`, `workDir`, `createdAt`,
  `updatedAt`). Resume: `kimi --session <sessionId>` (short `-S`);
  `kimi --continue` continues the cwd's latest; fork exists only as the
  in-session `/fork` command — no CLI fork argv. A running `kimi` REPL holds
  **no** fd on its session dir and exports no session env var (probed), so a
  bare `kimi` process cannot be bound to a session; a `kimi --session <sid>`
  process can, via argv.
- **Claude Code**: unchanged — transcripts + `sessions/<pid>.json` registry +
  `claude agents --json`, the richest liveness of the three.

## Decision

- **A `providers/` package is the CLI seam.** Each provider is a small typed
  module implementing one protocol: session discovery (disk → provider-
  neutral `Session` rows), liveness contribution, and argv synthesis
  (`resume_argv`, `new_session_argv`). A static registry maps provider key →
  implementation; a provider is active when its home directory exists
  (`~/.claude`, `~/.codex`, `~/.kimi-code`), restrictable via
  `CSCTL_PROVIDERS`. The existing `data/` pipeline is not moved: it becomes
  the Claude provider's engine behind the same protocol. Import direction is
  unchanged (`views` → `actions`/`providers` → `data`).
- **`Session.provider` is part of session identity.** Sids are unique only
  within a provider; every cross-layer lookup (action dispatch, execution-
  time resolution, window naming) carries the provider key. tmux windows for
  non-Claude sessions get a provider prefix (`cx-<sid8>`, `km-<sid8>`);
  Claude keeps bare `<sid8>` for continuity.
- **Capabilities gate verbs; nothing is emulated.** Providers declare typed
  capabilities (fork, takeover, background agents, RC, cleanup, liveness
  grade). The UI consults them: `f` fork covers Claude (`--fork-session`)
  and codex (`codex fork <sid>`) but not kimi (in-session `/fork` only);
  the Agents and Projects-RC surfaces stay Claude-only, and cleanup stays
  Claude-only — csctl does not delete state it does not fully model. A verb
  a provider cannot support is absent/refused with a typed detail, never
  approximated.
- **Argv-exact liveness for non-Claude providers.** Codex/kimi have no
  registry equivalent, so their liveness comes from a `/proc` cmdline scan:
  a process whose argv carries the session id (`codex resume <sid>`,
  `kimi --session <sid>`) is an exact, takeover-grade match — and because csctl's
  own tmux dispatch always resumes by id, **every session the workbench
  manages is exactly matchable by construction**. Bare-launched TUIs (plain
  `codex` / `kimi`, argv without a sid) cannot be bound to a session; their
  sessions read as not-alive and are never kill targets (fail-safe: resume
  of a secretly-running session collides visibly in the CLI's own UI, while
  a wrong SIGTERM would be unrecoverable). The R10 rule extends unchanged:
  no `/proc`, no destructive verbs.
- **Clarification (2026-08-04):** "matchable by construction" above covers
  only id-carrying RESUME dispatch — csctl resuming an existing session back
  into tmux, or a live takeover's execution-time re-scan. The Projects-tab
  launcher's NEW-session dispatch (`x`/`k`) uses each provider's bare
  `new_session_argv()` with no session id yet, so launcher-created sessions
  are unbound (same as any other bare-launched TUI) until later resumed by
  id. This note corrects only the claim's scope; the bullet above is left as
  originally decided.
- **Amendment (2026-08-04):** codex argv binding is stricter than "argv
  carries the session id". Only a real `resume` invocation binds: the target
  is the single token right after the `resume` token, and an `exec` token
  before `resume` never binds — a UUID inside a `codex exec` prompt is not
  evidence, and binding it would make the headless exec process a wrong
  SIGTERM target. The target may also be a session name (`codex resume
  <name>` — upstream help: "Session id (UUID) or session name"), resolved
  through `session_index.jsonl` only when that name maps to exactly one id;
  unknown or ambiguous names, flag-shaped targets (`codex resume --last`)
  and the bare picker stay unbound (fail closed). The bullet above is left
  as originally decided.
- **Amendment (2026-08-04, C1):** kimi 0.31.1 destroys its own argv evidence
  at runtime: an active REPL rewrites its process title, collapsing
  `/proc/<pid>/cmdline` to `kimi-code` plus whitespace padding — the
  `--session <sid>` tokens vanish (probed live against a csctl-dispatched
  session; comm follows the rewrite to `kimi-code`, exe still resolves to
  `~/.kimi-code/bin/kimi`; rewrite timing varies — a bare `kimi` cmdline was
  still intact a day earlier). "Every session the workbench manages is
  exactly matchable by construction" therefore no longer holds for kimi
  through argv alone. Dispatched sessions bind through csctl's OWN spawn
  metadata instead: every tmux dispatch declares `@csctl_sid` /
  `@csctl_provider` window user options at spawn (fork and launcher windows
  declare only the provider — their sid does not exist yet), and discovery
  joins a declaring pane to the pane root process — or its unique TUI-shaped
  descendant — whose identity matches the provider's process-identity set
  (kimi: argv0 `kimi`, comm `kimi-code`, or exe basename `kimi`), capturing
  `proc_start` at scan time so the kill-time recheck still defeats pid
  reuse. Argv bindings keep priority; the metadata is a supplement, so codex
  behavior with intact argv is unchanged. The binding fails closed: a
  missing option, an incomplete pane inventory, an identity or TUI-shape
  mismatch, a vanished pane process, and a sid claimed over distinct pids
  all bind nothing, and window NAMES never bind. The unbound-live hint uses
  the same identity set (a title-rewritten bare kimi hints again) and skips
  bound pids. The execution-time resolver re-gathers both sources fresh.
  Kimi's liveness grade is now `TMUX` (ARGV + dispatch metadata; bare TUIs
  stay blind). The bullet above is left as originally decided.
- **Takeover semantics generalize, with the same single decision point.**
  `should_kill = alive ∧ ¬current ∧ ¬fork` is provider-neutral;
  `take_over_result`'s kill-time recheck applies to any exact-matched pid.
  For non-Claude pids `proc_start` is captured from `/proc` at scan time
  (same pid-reuse defense, different source). The Claude-only execution-time
  resolver stays Claude's; codex/kimi resumes re-scan their argv index at
  execution time instead.
- **Discovery stays honest about cost and hiding.** Codex discovery reads
  only rollout first lines + the index (no full-file parses); subagent
  rollouts (`thread_source: subagent`) are SKIPPED entirely — they are
  internal execution artifacts, not operator work units, and their
  `session_id` points at the parent thread; `codex exec` headless runs
  (`originator: codex_exec`) map onto the existing SDK hide filter instead,
  as `claude -p` sessions do. Kimi discovery is index + `state.json` only.
  Provider source failures surface as non-fatal typed issues (unreadable
  subtrees, malformed `state.json`, unparseable rollout heads) — degraded
  sources narrow the list visibly, never silently.
- **Amendment (2026-08-04):** "reads only rollout first lines" has narrowed
  to "bounded reads, never full-file parses": when neither the index nor
  the first line yields a label, codex discovery continues reading the
  rollout body for the first user message under hard caps (64 lines /
  128 KB). The bullet above is left as originally decided.
- **The launcher goes multi-CLI; membership does not (yet).** The Projects
  tab keeps Claude-trust membership (ADR-0003) but its launcher offers a new
  session per active provider; each CLI shows its own trust/onboarding
  dialog inside the tmux window, exactly like the existing no-trust-gate
  argument for `do_tmux_new_result`. Merging codex `config.toml`
  `projects.*` and kimi `workspaces.json` into membership is deferred to the
  RC-membership redesign (0.9+).

## Consequences

- The Sessions tab becomes the machine-wide session surface for all three
  CLIs (with a provider column and filter); Agents and Projects-RC remain
  Claude surfaces until those CLIs grow equivalent primitives.
- Non-Claude liveness is deliberately conservative: a codex/kimi session
  resumed outside csctl by hand (`codex resume <sid>` in a bare terminal) is
  still exactly matched; only bare fresh TUIs stay unbound. This is the same
  spawn-vs-discovery tradeoff claude-squad resolves by managing only its own
  spawns — csctl discovers everything but only *binds* what argv proves.
- Desktop/IDE-hosted codex sessions (app-server-held rollouts) currently
  read as not-alive. Surfacing them as "hosted, read-only" via the
  app-server's fd table is a known follow-up, not in this change.
- A copied resume command for a LIVE non-Claude session is the plain
  provider resume (`codex resume <sid>`): the Claude-only
  `csctl resume --take-over` deferral cannot cover it, and a plain resume
  serializes no kill — the CLI itself surfaces any collision. The launcher
  binds one explicit key per provider (`x`/`k`) — a deliberate UI choice at
  three CLIs, revisited if the registry grows.
- `codex fork <sid>` argv deliberately does NOT bind liveness: the fork
  process is minting a NEW session, so binding the parent sid to the fork's
  pid would make the parent a wrong takeover target.
- Upstream contracts multiply: codex/kimi disk formats and resume flags
  join the compatibility checklist re-verified per release.
