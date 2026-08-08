# Claude Code compatibility verification

csctl parses local state and command output owned by Claude Code. Treat those
shapes as an upstream compatibility contract and rerun this checklist before
each release; unit fixtures prove csctl's response to known shapes, not that a
new Claude Code version still emits them.

## Last recorded evidence

| Scope | Claude Code | Date | Evidence |
|---|---:|---:|---|
| Trust inheritance and `~/.claude.json` footprint | 2.1.218 | 2026-07-23 | Parent `true` suppresses the child dialog; the child may be recorded with explicit `false`; declining writes no project entry |
| Isolated read-only command probe | 2.1.226 | 2026-08-08 | For the v0.8.4 release: `claude --version` exited 0; `claude agents --help` exited 0 with `--json`; `claude agents --json` exited 0 with valid-JSON `[]` under an empty temporary home; unauthenticated `remote-control --help` stopped at the login boundary without starting a server |
| Authenticated `remote-control --help` | 2.1.226 | 2026-08-08 | Reused the maintainer's existing Linux Claude.ai login for this read-only probe: exit 0, with `--name`, `--spawn`, and `same-dir`; the credential file hash was unchanged and no server was started |

The 2.1.226 release probes did **not** revalidate trust inheritance or non-empty
registry fields. Do not advance the semantic “last verified” version from
2.1.218 until the remaining disposable-fixture steps below pass.

## Tier 1: isolated read-only probe

Run from the repository. It never reads the operator's real `~/.claude` and
does not start a Remote Control server. Keep the printed temporary path with
the release evidence and remove it after inspection.

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
run_read_only_probe remote-control-help claude remote-control --help

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
  only the command envelope, not entry fields);
- `claude remote-control --help` never starts a server. In an unauthenticated
  home, record the nonzero/authentication diagnostic rather than treating it
  as proof of the authenticated help contract.

Also run csctl's isolated compatibility fixtures:

```bash
uv run --extra dev pytest \
  tests/test_trust.py \
  tests/test_project_settings.py \
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
- `~/.claude/jobs/<short>/state.json` is an object with `sessionId`; record the
  observed shapes of `resumeSessionId`, `state`, `tempo`, `cwd`, `name`,
  `respawnFlags`, and optional `bridgeSessionId`. Confirm that the job record
  itself still has no host pid.
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
4. Run the focused csctl tests above to confirm unreadable, malformed, invalid,
   or unknown project settings produce `TrustDecision.UNAVAILABLE` and RC
   startup is refused before tmux/Claude invocation.

If steps 1–3 differ from the recorded 2.1.218 semantics, stop the release and
update the trust model, tests, ADR-0003, and `CONTEXT.md` together.

### Remote Control help and exit contract

In the authenticated disposable home, run only:

```bash
claude remote-control --help
printf 'exit=%s\n' "$?"
```

It must exit 0 and document the `--name` and `--spawn` options used by csctl,
including the `same-dir` spawn value. Do **not** invoke `claude remote-control`
without `--help`; that would start a real server and may create cloud/network
side effects. The unauthenticated Tier 1 result is useful evidence of the auth
boundary but does not satisfy this gate.

## Release decision and fallback

Block the release when a required field changes type or disappears, agents
JSON stops being an array, trust inheritance changes, or the authenticated
Remote Control help/exit contract cannot be proven. Do not weaken parsing or
guess a replacement field.

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
| Codex TUI holds NO fd on its rollout; app-server holds MANY rollouts | 0.146.0 | 2026-08-04 | Live `/proc/<pid>/fd` probes (post-prompt) |
| Kimi `session_index.jsonl` (`sessionId` / `sessionDir` / `workDir`) + per-session `state.json` (`title` / `lastPrompt` / `workDir` / `updatedAt`) | 0.34.0 | 2026-08-08 | Latest index and state rows retained the required nonempty string fields |
| `kimi --session <id>` / `-S` resume; no CLI fork (in-session `/fork` only) | 0.34.0 | 2026-08-08 | `kimi --help` |
| Kimi REPL exposes NO fd/env session binding | 0.31.1 | 2026-08-04 | Live `/proc/<pid>/{fd,environ}` probes |
| Kimi runtime REWRITES its own process title: an active dispatched session's cmdline collapses to `kimi-code` + whitespace padding — the `--session <sid>` argv is destroyed; comm = `kimi-code`, exe → `~/.kimi-code/bin/kimi` | 0.31.1 | 2026-08-04 | Live `/proc/<pid>/{cmdline,comm,exe}` of a csctl-dispatched session (pid 3587394, window `km-2661a1d4`); a bare `kimi` process observed 2026-08-03 still showed cmdline `kimi` (rewrite timing varies) |

Re-verify with the same read-only probes (`--help` outputs, first-line
samples, `/proc` fd/cmdline/comm/exe checks against a live TUI). The
tmux window-metadata binding (`@csctl_sid`/`@csctl_provider`, ADR-0005 C1
amendment) and kimi's process-identity set (comm `kimi-code` / exe basename
`kimi`) key on the title-rewrite observation above — re-verify it per
release. A provider whose contract breaks degrades to typed provider issues
in the Sessions status line — it must never blank the Claude view; adapt
the owning provider module with new fixtures before release.
