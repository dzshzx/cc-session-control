# Surface reduction to the operator core

Status: accepted (2026-07-30); RC and background-agent management clauses
partially superseded by ADR-0009

By 0.7.6 csctl carried several surfaces built for operators it does not have:
a bridge-environment ledger, a headless CLI mirroring most TUI verbs, an RC
autostart list, anti-TOCTOU removal hardening against a hostile local user,
and terminal-background auto-detection. The usage evidence is narrow: the
operator provably works TUI-only, the autostart list on the deployment
machine was empty, and the only headless consumer is the
claude-session-doctor skill. 0.8.0 deletes what that evidence does not
justify. This extends ADR-0001: with Remote Control demoted to a secondary
surface, the bookkeeping and automation that only served RC lost their
remaining justification.

## Decision

- **Drop the bridge-environment ledger pipeline.** `environments.py`,
  `environment_ledger.py`, `rc_environment.py`, the `csctl env` subcommand,
  and `$XDG_CONFIG_HOME/csctl/environments.jsonl` are removed. The
  pipeline's only TUI output was an anomaly count about the ledger itself;
  csctl has no way to deregister a cloud environment, so the ledger could
  never become actionable; and its orphan list was incomplete by its own
  admission (environments minted while csctl was not running were never
  recorded). Bridge Environment stays a modeled concept
  (`session_*`/`cse_*` namespaces, `is_rc_exposed`, the agent env suffix) —
  csctl just keeps no history of it. Cloud-side cleanup stays manual on
  claude.ai/code, now without a partial local list.
- **Narrow the headless CLI to `resume` + `agents`.** The TUI is the human
  surface; the headless CLI is the agent-facing one, and its real consumer —
  the claude-session-doctor skill — needs exactly `csctl resume` (including
  `--take-over`) and `csctl agents`. The `prune`, `env`, `skill`, and `rc`
  subcommands duplicated TUI capabilities the operator already uses there
  and are removed: with `prune` gone, the Sessions cleanup submenu — which
  already carried all five plan-frozen actions, including the zombie and
  age sweeps — becomes the sole cleanup surface, and the Projects tab the
  sole RC surface.
  The bundled skill moved to the dzshzx/agent-skills repository and installs
  via the skills CLI instead of `csctl skill install`.
- **Retire the autostart-list feature.** The `rc-enabled` list, its
  `csctl rc up` starter, the Projects `a`/`A` keys, and the 开机自启 column
  solved a fleet-boot problem this deployment does not have: the list was
  empty, and RC start is on-demand (`o`) by design once ADR-0001 made RC
  secondary.
  `rc_enabled.py` — including the frozen pre-0.7.3 short-name migration
  (`_legacy_workspace_root`) — is deleted; a leftover
  `$XDG_CONFIG_HOME/csctl/rc-enabled` file is inert. The per-project
  `remoteControlAtStartup` flag (`c`) is unaffected — it belongs to Claude
  Code, not csctl.
- **Downgrade removal hardening to lstat identity revalidation.**
  Threat-model ruling: csctl is a single-operator panel over that operator's
  own `~/.claude`; "a local attacker swaps paths between preview and
  execute" is out of scope. The `renameat2(RENAME_NOREPLACE)` claim/rollback
  and the fd-chain `O_NOFOLLOW` root walk are deleted. Three protection
  layers remain, aimed at accidents rather than attackers: business
  revalidation on fresh evidence (delete ⊆ preview), root containment, and
  lstat identity (device / inode / file type) frozen with the plan and
  re-checked at execution — any mismatch, including a target that became a
  symlink, is REFUSED.
- **Drop OSC 11 background auto-detection.** Under tmux-first the only
  beneficiary of the OSC 11 query (with its DA1 sentinel and pty-driven
  tests) was a bare-terminal first launch; most terminals never answer, so
  every startup paid for a probe that rarely produced a verdict.
  `detect_mode()` is now `cfg.theme` / `CSCTL_THEME` / `--theme` →
  `$COLORFGBG` → dark; light-terminal users set the override once.

## Relation to earlier ADRs

- **ADR-0001** is extended, not revised: tmux-first demoted Remote Control,
  and this ADR removes the RC-adjacent bookkeeping (ledger, autostart list)
  that the demotion left without a payoff.
- **ADR-0003** is partially superseded: its ledger-diagnostics rules and the
  rc-enabled transaction semantics (`EnabledListResult`, committed-unlock
  reporting) describe deleted code. Its surviving principle — typed,
  fail-closed results at every boundary — continues to govern the trust,
  settings, cleanup, refresh, and write boundaries unchanged.

## Consequences

- Scripts calling the removed subcommands break loudly at argparse; the
  remaining headless contract is deliberately small and agent-shaped.
- Leftover state files (`environments.jsonl`, `rc-enabled`) are inert and
  can be deleted by hand; csctl neither reads nor rewrites them.
- A deployment that ever needs fleet autostart, cloud-environment history,
  or hostile-local-user hardening must consciously bring the mechanism back
  against this ADR rather than inherit it as dead weight.
