# Evidence-tier project membership

Status: accepted (2026-08-12). RC-window hygiene, RC-start-gate, and
badge-rendering clauses are superseded by ADR-0009. Supersedes the
membership-listing half of ADR-0003 (its trust predicate remains) and resolves
the membership merge ADR-0005 deferred to "the RC-membership redesign (0.9+)".

Projects-tab membership was single-source: the effectively-trusted keys of
`~/.claude.json`, minus temp roots and dead directories, plus an rc-window
escape. ADR-0005 made the launcher multi-CLI but not membership, so a
directory where the operator only ever runs codex or kimi was invisible on
the tab and unusable as a launch point, and the operator had no way to add
or suppress a directory.

## Decision

A **project is an absolute directory path plus a provenance evidence set**.
Membership is the union of three evidence tiers, minus hygiene rules, with
operator curation on top (`data/membership.py`):

- **Pinned** — the operator pinned the directory in csctl's own curation
  store (`data/curation.py`, `cfg.curation_file`). Immune to hygiene and
  decay; the only tier csctl writes.
- **Trusted** — a provider's trust store covers the directory. Claude keeps
  the ADR-0003 effective-trust semantics (ancestor inheritance via
  `models.effective_trust_decision`); codex contributes
  `config.toml [projects.*] trust_level = "trusted"` keys and kimi
  contributes `workspace-trust/<id>` record roots, both **exact-match only**
  — whether those CLIs' runtime trust inherits down the tree is unverified
  upstream, so csctl conservatively does not re-derive it.
- **Observed** — any provider has session activity (cwd) in the directory.
  An observed-only directory decays out of the tab after 30 days without new
  activity (`OBSERVED_DECAY_DAYS`, a deliberate constant). Decay only
  affects the Projects tab; the Sessions tab remains the exhaustive activity
  surface.

Hygiene rules (temp roots; missing directories) apply to Trusted/Observed
and are waived for pinned entries and entries holding an rc window — the
pre-existing escapes. A **hidden** directory (the second curation list) is
suppressed regardless of evidence; hidden rows still ship in the scan so the
view's show-hidden mode can offer the unhide verb.

The load-bearing invariant: **trust inheritance qualifies recorded
candidates; it never generates them.** Candidates come only from explicit
records (trust-store keys, pins) and observed activity, so a trusted `/` —
which exists in real codex configs — cannot flood the tab.

Further rules:

- kimi's `workspaces.json` is deliberately NOT read: its roots are the
  activity evidence the session index already provides with better mtimes.
- csctl never writes any provider's trust store. The curation store is the
  single csctl-owned source: JSON, advisory-locked read-modify-write, atomic
  replace, foreign keys preserved, a broken store never clobbered.
- Membership never feeds destructive operations: cleanup still models Claude
  state only, and the RC start gate still requires Claude effective trust
  (`trust_decision`) regardless of which other tiers list the directory.
- Each new source fails independently into typed `membership_issues`; a
  degraded codex/kimi/curation source narrows the tab, never blanks it.
- Rows carry their provenance (`trusted_by` / `observed_by` / `pinned` /
  `hidden`) and the tab shows it as 钉/隐/信cc/信cx/信km/活… badges;
  信cc is exactly the RC-start-gate predicate. Ordering is pinned-first,
  then activity.

## Consequences

- Migration is purely additive: every previously listed directory still
  qualifies (Claude trust tier or rc-window escape), so no row vanishes.
- codex/kimi-only directories become visible and launchable; each CLI's own
  onboarding/trust dialog still runs inside the tmux window (ADR-0005's
  no-trust-gate argument now covers the new members too).
- Upstream items rechecked on 2026-08-17: codex 0.147.0 does not walk arbitrary
  ancestors; it exact-matches normalized cwd, configured project-root, and Git
  repo-root keys, so csctl's explicit-record candidate rule remains correct.
  Kimi 0.36.1 `untrust()` deletes its positive `workspace-trust` document and
  leaves no decline/revoke record. A missing record is therefore untrusted;
  a record without well-typed `root`/`trustedAt` is skipped with an issue.
