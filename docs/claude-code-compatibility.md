# Claude Code compatibility verification

csctl parses local state and command output owned by Claude Code. Treat those
shapes as an upstream compatibility contract and rerun this checklist before
each release; unit fixtures prove csctl's response to known shapes, not that a
new Claude Code version still emits them.

## Last recorded evidence

| Scope | Claude Code | Date | Evidence |
|---|---:|---:|---|
| Trust inheritance and `~/.claude.json` footprint | 2.1.218 | 2026-07-23 | Parent `true` suppresses the child dialog; the child may be recorded with explicit `false`; declining writes no project entry |
| Isolated read-only command probe | 2.1.228 | 2026-08-12 | For the v0.8.5/v0.8.6 releases (identical same-day results): `claude --version` exited 0; `claude agents --help` exited 0 with `--json`; `claude agents --json` exited 0 with valid-JSON `[]` under an empty temporary home |
| Isolated read-only command probe | 2.1.231 | 2026-08-13 | For the v0.8.8 and v0.8.9 releases (identical same-day results): `claude --version` exited 0; `claude agents --help` exited 0 and still advertises `--json`; `claude agents --json` exited 0 with valid-JSON `[]` under an empty temporary home |
| Isolated read-only command probe | 2.1.233 | 2026-08-16 | For the v0.8.10 release: `claude --version` exited 0; `claude agents --help` exited 0 and still advertises `--json`; `claude agents --json` exited 0 with valid-JSON `[]` under an empty temporary home |
| Isolated read-only command probe | 2.1.233 | 2026-08-17 | For the v0.8.12 release: `claude --version`, `claude agents --help`, and `claude agents --json` all exited 0 under an empty temporary home; help still advertised `--json`, whose output parsed as a JSON array (`[]`) |
| Isolated read-only command probe | 2.1.239 | 2026-08-22 | Post-v0.8.15 ADR/CONTEXT audit (no release): Tier 1 script rerun in a temp HOME — `claude --version`, `claude agents --help` (still advertises `--json`) and `claude agents --json` all exited 0 and the JSON parsed; temp footprint `.claude/backups` + `.claude.json` only; the full suite (1041 tests, incl. the isolated fixtures) passed. Trust-inheritance semantics (Tier 2) not re-run. |
| Isolated read-only command probe | 2.1.234 | 2026-08-18 | For the v0.8.13 release: `claude --version`, `claude agents --help`, and `claude agents --json` all exited 0 under an empty temporary home; help still advertised `--json`, whose output parsed as a JSON array (`[]`) |
| Disposable authenticated fixture | 2.1.233 | 2026-08-17 | Temporary HOME/XDG roots plus a process-only OAuth token; one foreground and one idle background session produced well-typed registry/agents rows; parent trust was `true`, a new child inherited without a dialog and recorded explicit `false`, and declining an unrelated directory wrote no project entry; real `~/.claude.json` and credential hashes stayed unchanged |

The 2.1.233 disposable fixture revalidated the semantic contract previously
held at 2.1.218. Its foreground `sessions/<pid>.json` carried numeric `pid`,
string `sessionId`/`cwd`/`procStart`/`kind`/`entrypoint`/`status`, and optional
fields remained optional; the background row used `kind: "bg"`. `claude agents
--json --all` remained an array whose relevant rows had numeric `pid`, string
`sessionId`/`cwd`/`kind`/`status`, plus additive fields on background rows.

## Tier 1: isolated read-only probe

Run from the repository. It never reads the operator's real `~/.claude` and
does not start an interactive Claude session. Keep the printed temporary path
with the release evidence and remove it after inspection.

```bash
compat_root="$(mktemp -d)"
compat_home="$compat_root/home"
mkdir -p "$compat_home"

date -Iseconds >"$compat_root/checked-at.txt"
run_read_only_probe() {
  probe_name=$1
  shift
  if env HOME="$compat_home" XDG_CONFIG_HOME="$compat_root/config" \
    XDG_DATA_HOME="$compat_root/data" \
    "$@" >"$compat_root/$probe_name.txt" 2>&1
  then
    probe_status=0
  else
    probe_status=$?
  fi
  printf '%s\n' "$probe_status" >"$compat_root/$probe_name.exit"
}

run_read_only_probe version claude --version
run_read_only_probe agents-help claude agents --help
run_read_only_probe agents-json claude agents --json

if python -m json.tool "$compat_root/agents-json.txt" >/dev/null
then
  agents_json_status=0
else
  agents_json_status=$?
fi
printf '%s\n' "$agents_json_status" >"$compat_root/agents-json-parse.exit"
printf 'compatibility evidence: %s\n' "$compat_root"
```

Record the version, ISO timestamp, every exit status, and raw stdout/stderr.
The Tier 1 gate requires:

- `claude agents --help` exits 0 and still advertises `--json`;
- `claude agents --json` exits 0 and is a JSON array (an empty array verifies
  only the command envelope, not entry fields).

Also run csctl's isolated compatibility fixtures:

```bash
uv run --extra dev pytest \
  tests/test_trust.py \
  tests/test_project_settings.py \
  tests/test_membership.py \
  tests/test_registry.py \
  tests/test_cli_entry.py
```

These tests use temporary paths and injected subprocess results; they must not
point at the real Claude home or tmux.

## Tier 2: disposable authenticated fixture

Use a throwaway OS account, VM, or container with its own home and Claude Code
login. Do not use the maintainer's real `~/.claude`, and do not run these
experiments in a real project. Capture sanitized copies of the following
artifacts with the version and date.

### Field shapes

Create one foreground session and one background agent in the fixture, then
check:

- `claude agents --json` is a list of objects. Each relevant row has a nonempty
  string `sessionId`; `pid` is either an integer or null/absent for a settled
  entry.
- `~/.claude/sessions/<pid>.json` is an object with an integer-convertible
  `pid`, nonempty `sessionId`, and the observed shapes of `cwd`, `kind`,
  `entrypoint`, `status`, `procStart`, and optional `bridgeSessionId`.
- `~/.claude.json` is an object whose `projects` member is an object, project
  values are objects, and any `hasTrustDialogAccepted` value is boolean.

Do not infer a field from filenames or old fixtures: record what the candidate
version actually writes.

### Trust inheritance and unknown evidence

In disposable parent and child directories:

1. Accept the trust dialog at the parent and record the parent project entry.
2. Start Claude Code from a new child. Confirm that no dialog appears and
   record whether the child entry is absent or carries explicit `false`.
3. From an unrelated directory, decline the dialog and record whether any
   project entry is written.
4. Run the focused csctl tests above to confirm unreadable, malformed, or
   invalid project settings preserve their typed unavailable state and surface
   a Projects membership-source issue instead of manufacturing Claude trust.

If steps 1–3 differ from the recorded 2.1.218 semantics, stop the release and
update the trust model, tests, ADR-0003, and `CONTEXT.md` together.

## Release decision and fallback

Block the release when a required session-registry field changes type or
disappears, agents JSON stops being an array, or trust inheritance changes. Do
not weaken parsing or guess a replacement field.

Optional additive fields are compatible when existing fixtures and the full
quality gate still pass. For an incompatible candidate, keep the last supported
Claude Code version documented, fail closed where authority is uncertain, and
adapt the owning boundary with new fixtures before release. Update this
document and `CONTEXT.md` only with evidence actually captured; record any
unverified item explicitly.

## Non-Claude provider contracts (ADR-0005)

The provider layer parses Codex CLI and Kimi Code on-disk state and resume
argv shapes. Treat these as the same class of upstream contract and re-verify
per release (read-only probes; never write into a CLI's real home):

| Scope | Version | Date | Evidence |
|---|---:|---:|---|
| Codex rollout layout + `session_meta` first line (`payload.id` == `payload.session_id` for `thread_source: "user"`; subagent rollouts carry the parent thread id) | 0.146.0 | 2026-08-04 | Sampled `~/.codex/sessions/**/rollout-*.jsonl` first lines |
| Codex `session_index.jsonl` (`id` / `thread_name` / `updated_at`) | 0.147.0 | 2026-08-08 | Latest index row retained all three nonempty string fields |
| `codex resume <SESSION_ID>` / `codex fork <SESSION_ID>` accept a UUID | 0.147.0 | 2026-08-08 | `codex resume --help`, `codex fork --help` |
| Codex TUI holds NO fd on its rollout; app-server holds MANY rollouts, which is exact evidence for a hosted/read-only row but never a session-owning pid | 0.147.0 | 2026-08-17 | Live `/proc/<pid>/fd` probe: the local app-server held 18 exact active rollout paths, including the current parent/child threads; ordinary TUI process evidence remained separate |
| Codex has NO home-relocation FLAG: `--codex-home` does not exist, and `-p/--profile` only layers `$CODEX_HOME/<name>.config.toml` WITHIN one home. `CODEX_HOME` is the only way to select a state home (ADR-0008 rests on this) | 0.147.0 | 2026-08-13 | `codex --help` full option list |
| A running codex process carries its home in `CODEX_HOME` (absent = codex's `~/.codex` default); two identities are otherwise indistinguishable, since a launcher that `exec`s codex leaves argv0 as plain `codex` | 0.147.0 | 2026-08-13 | `/proc/<pid>/environ` of live codex processes (app-server pids 516008/602046 → `/home/ubuntu/.codex`; one Windows-side process → its own home) |
| Kimi `session_index.jsonl` (`sessionId` / `sessionDir` / `workDir`) + per-session `state.json` (`title` / `lastPrompt` / `workDir` / `updatedAt`) | 0.34.0 | 2026-08-08 | Latest index and state rows retained the required nonempty string fields |
| `kimi --session <id>` / `-S` resume; no CLI fork (in-session `/fork` only) | 0.34.0 | 2026-08-08 | `kimi --help` |
| Kimi REPL exposes NO stable fd/env session binding; the wire-log fd 0.34.0 holds is transient (open around writes only) | 0.34.0 | 2026-08-12 | Live `/proc/<pid>/{fd,environ}` probes of two csctl-dispatched sessions (pids 2423524, 2460093): no env var; the `agents/main/wire.jsonl` fd appeared and disappeared within one turn |
| Kimi runtime REWRITES its own process title, destroying the `--session <sid>` argv: 0.31.1 collapsed cmdline to `kimi-code` + whitespace padding (comm `kimi-code`); 0.34.0 collapses to bare `kimi` (comm `kimi`); exe → `~/.kimi-code/bin/kimi` both | 0.31.1, 0.34.0 | 2026-08-12 | Live `/proc/<pid>/{cmdline,comm,exe}` of csctl-dispatched sessions (0.31.1: pid 3587394, window `km-2661a1d4`, 2026-08-04; 0.34.0: pids 2423524/2460093, windows `dingtalk-automation/kimi`, `cc-session-control/kimi`) |
| Kimi registers a NEW session at its FIRST PROMPT, not at startup; `kimi --session <unknown-id>` refuses to start (`Session ... not found`, exit 1) — csctl can neither mint nor learn a new session's sid at spawn (the late-sid gap the hook runtime registry closes) | 0.34.0 | 2026-08-12 | Bare `kimi` under a trusted dir with stdin `/dev/null` wrote no index line and no session dir within 8s; a probe id `session_deadbeef-…` failed with the refusal above and left no state |
| Kimi hook contract for the runtime registry: `SessionStart` fires when a session materializes (TUI first prompt; immediately for `--prompt`) with payload `session_id`/`cwd`/`source` (startup\|resume); ~~`SessionEnd` fires on clean `/exit` with `reason`~~ (**refuted on 0.35.0 — see the row below**); the hook process is kimi's grandchild (kimi → `sh -c` → hook) | 0.34.0 | 2026-08-12 | Temporary `[[hooks]]` probe writing ppid/payload to a log, driven through TUI (tmux), `-p`, and `--session` resume; config restored byte-identical afterwards |
| Kimi hook contract re-verified, with one claim REFUTED: `SessionStart` still fires on both paths and payload keys stay snake_case (0.35.0 assembles them camelCase and runs `toHookInputData`/`camelToSnake` before spawning the command); the grandchild ancestry holds. `SessionEnd` does **not** dependably fire — `run/<pid>.json` survived a clean `kimi -p` exit and a killed TUI, so entries accumulate and csctl must prune them | 0.35.0 | 2026-08-13 | Live runs against the operator's own config: `kimi -p` wrote `run/3298716.json` matching its printed sid; a tmux TUI probe wrote `run/3299643.json` on its first prompt; both entries remained after exit/kill. Refuted claim cross-checked against the binary's `toHookInputData`/`camelToSnake` and `HOOK_EVENT_TYPES` strings |
| A `--session` RESUME registers IMMEDIATELY — the first-prompt wait applies only to NEW sessions (whose sid does not exist before then). Also: the title rewrite has two live shapes within one version, chosen by launch path, not by version — a new 0.35.0 session collapsed to bare `kimi`, a resume of the same version to `kimi-code` | 0.35.0 | 2026-08-13 | Reopened a real 491k-context session with `kimi --session <sid>` in a tmux window: `run/3367729.json` carried the correct sid before any input was sent, and `/proc/<pid>/comm` read `kimi-code` where the pre-restart new-session process had read `kimi` |
| Kimi hook contract on 0.36.1: `SessionStart` still fires at a new TUI's first prompt (comm collapses to bare `kimi` on that path, as recorded); `SessionHeartbeat` — fires every 60 s while the session lives, only when configured — carries the same payload shape and re-registers through the same write path, and does NOT fire sessionless; `SessionEnd` now removes the entry on a clean `/exit` (0.35.0 did not). One `SessionStart` delivery was also observed silently never happening — the reason the heartbeat rule exists | 0.36.1 | 2026-08-16 | Live tmux TUI probes: first-prompt registration wrote `run/<pid>.json` with matching `procStart`; the entry was rewritten 62 s later with no input; a 70 s idle pre-prompt TUI wrote no entry and no error-log line; `/exit` removed the entry. The missed delivery: a dispatched NEW session ran 14 turns over two hours with no entry and no log line while the same path registered fine minutes before and after |
| Codex `session_meta` originator/source split: Codex Desktop main threads carry `originator: "Codex Desktop"` + `source: "vscode"` (Desktop reuses the IDE pipeline — only `originator` distinguishes it from real `codex_vscode` sessions) | 0.130.0–0.147.0 | 2026-08-12 | Sampled every `~/.codex/sessions/**/rollout-*.jsonl` first line: 14 desktop rows with `source: "vscode"` |
| Codex project trust is not arbitrary ancestor inheritance: 0.147.0 checks exact normalized keys for the cwd, configured project root, and resolved Git repository root; a subdirectory in the same repository therefore uses the repository-root record | 0.147.0 | 2026-08-17 | Official `rust-v0.147.0` source: `config/src/loader/mod.rs::ProjectTrustContext::decision_for_dir` and `tui/src/onboarding/trust_directory.rs` |
| Kimi workspace trust has only a positive record: `trust()` writes `{root,trustedAt}` under `workspace-trust`; `untrust()` deletes that document and flips runtime state false, so decline/revoke leaves no negative footprint | 0.36.1 | 2026-08-17 | Installed official binary's bundled source (`workspaceTrustService.ts`) plus the official `/workspaces/{workspace_id}/untrust` route; no operator state mutated |
| Codex 0.149.0 still has NO `--codex-home` flag; `codex resume` subcommand present (picker by default, `--last`) | 0.149.0 | 2026-08-22 | Post-v0.8.15 audit: `codex --help` (0 matches for `--codex-home`), `codex resume --help` — read-only; rollout/session_index shapes and the app-server fd behaviour NOT re-sampled |
| Kimi 0.38.0 keeps `--session` / `--prompt`; the operator config registers all three hook events (`SessionStart`, `SessionHeartbeat`, `SessionEnd`) to `csctl _kimi-hook` | 0.38.0 | 2026-08-22 | Post-v0.8.15 audit: `kimi --help`, `~/.kimi-code/config.toml` hook blocks (names only) — read-only; heartbeat cadence, payload key case, title-rewrite shapes and first-prompt registration NOT re-verified (need a live session) |
| opencode 1.18.15 keeps `--session` / `--fork` / `--prompt` and the `session delete` verb; `opencode.db` present at the XDG data path | 1.18.15 | 2026-08-22 | Post-v0.8.15 audit: `opencode --help`, `opencode session --help`, path test — read-only; `session` table shape not re-sampled. First row for opencode in this table (ADR-0005 amendment 2026-08-18 required one) |

Re-verify with the same read-only probes (`--help` outputs, first-line
samples, `/proc` fd/cmdline/comm/exe checks against a live TUI). The
tmux window-metadata binding (`@csctl_sid`/`@csctl_provider`, ADR-0005 C1
amendment) and kimi's process-identity set (comm `kimi-code` / exe basename
`kimi`) key on the title-rewrite observation above — re-verify it per
release. A provider whose contract breaks degrades to typed provider issues
in the Sessions status line — it must never blank the Claude view; adapt
the owning provider module with new fixtures before release.
