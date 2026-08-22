# Upstream settings, effective trust, and typed diagnostics

Status: accepted (2026-07-29); partially superseded by ADR-0004 (2026-07-30),
which removed the ledger-specific surfaces while retaining the typed,
fail-closed boundary rules. RC start, settings-write, and lifecycle clauses
are superseded by ADR-0009.
Membership-listing half superseded by ADR-0007 (the effective-trust predicate remains).
Read-only re-check 2026-08-22 on Claude Code 2.1.239 (Tier 1 green; Tier 2 trust semantics last
re-verified on 2.1.233 — see `docs/claude-code-compatibility.md`).

csctl depends on Claude Code state that it does not own, including
`~/.claude.json`, per-project `.claude/settings.local.json`, session/job
registries, and the `claude` CLI. Missing state is normal on a new machine;
unreadable, malformed, or structurally incompatible state is evidence loss,
not proof that a project is untrusted or an operation succeeded.

## Decision

### Project settings and effective trust

- `data/project_settings.py` is the typed boundary for Claude project metadata.
  A missing `~/.claude.json` is an available empty project map. Unreadable,
  malformed, and invalid documents retain distinct states and details.
- `models.effective_trust_decision` is the single trust predicate. Its result is
  tri-state: `TRUSTED`, `UNTRUSTED`, or `UNAVAILABLE`. RC startup requires
  `TRUSTED`; unavailable evidence fails closed before tmux or Claude Code is
  invoked.
- Effective trust is inherited from any path-segment ancestor whose project
  entry has `hasTrustDialogAccepted: true`. An explicit `false` on a child does
  not override a trusted ancestor. Paths use `normpath`, not `realpath`, to
  match Claude Code's literal-cwd records.
- This inheritance and the `projects` map shape are upstream semantics, not a
  format controlled by csctl. They were last semantically verified against
  Claude Code 2.1.218 on 2026-07-23 and must be rechecked with
  [the release compatibility checklist](../claude-code-compatibility.md).
- Writes to `remoteControlAtStartup` preserve unrelated JSON keys and use a
  dedicated advisory lock plus temporary-file, `fsync`, and atomic replace.
  `SettingWriteResult` distinguishes updated, unchanged, and the exact failed
  boundary so the CLI/TUI can report the real outcome.

### Domain-specific diagnostics

- Ledger reads and updates preserve missing, partial, failed, malformed-row,
  and exact persistence-boundary information. A partial ledger may still
  report current observations, but it marks history incomplete and surfaces
  warnings.
- Cleanup returns per-path removed, missing, failed, skipped, and refused
  outcomes. Aggregate success is never inferred from merely attempting the
  operation; CLI exit status and TUI notices reflect incomplete work.
- Refresh, trust, project-setting, ledger, cleanup, RC lifecycle, and TUI
  action boundaries keep their own typed results. We deliberately do not use
  one global `Result` type: these domains have different valid states,
  diagnostics, partial-success data, and safety decisions. A universal result
  would either erase distinctions or accumulate fields irrelevant to most
  callers.
- Presentation adapters translate domain results to English CLI diagnostics
  or Simplified Chinese TUI notices. Compatibility bool/list wrappers exist
  only for older callers and retain fail-closed behavior; new safety decisions
  use the typed result.

## Consequences

- A corrupt or newly incompatible `~/.claude.json` cannot silently authorize
  an RC start. The operator sees why trust evidence is unavailable.
- Missing first-run state stays usable without being mislabeled as an I/O
  failure.
- Partial cleanup and ledger degradation remain actionable without being
  presented as complete success.
- Release verification must distinguish what can be proven by isolated
  read-only CLI probes from trust and registry semantics that require a
  disposable authenticated fixture.
