# Unified interactive tmux session

Status: accepted (2026-08-08); RC/background-agent placement clauses
partially superseded by ADR-0009;
ADR-0011 adds the scoped second prefix (`prefix2` = `C-a`) to the managed session's
create/reuse path.

## Context

ADR-0001 made tmux residency the primary disconnect-survival mechanism and
placed each project's agent windows in a separate tmux session. That grouping
kept project names visible, but made phone terminals pay a second navigation
step: switching projects required switching tmux sessions before choosing a
window. The workbench already discovers residency globally with
`list-panes -a`, and project identity is the absolute cwd rather than the tmux
session or window name.

## Decision

- Every agent session csctl dispatches uses one tmux session named `csctl`:
  new sessions, resumes, forks, Sessions `R` backgrounding, and background-agent
  respawns all create windows there.
- Window names keep the project visible as `<project>/<leaf>`. `<leaf>` is the
  provider key for a brand-new session, the provider-owned SID/fork name for a
  resume, or the background-agent name. Names are display-only; exact tmux
  targets and identity metadata remain authoritative.
- Managed Remote Control servers remain in the separate configurable `rc`
  session. `CSCTL_RC_SESSION` accepts a literal name, rejects tmux target
  expression syntax, and must differ from `csctl`; mixing RC and agent windows
  would make RC inventory and stop-all unsafe. Existing session/window names are
  always addressed with tmux's exact-match `=` form, never prefix/glob fallback.
  Project window names and the `@csctl_path` join are unchanged.
- A session already resident in any tmux session is entered in place. Existing
  per-project or user-created sessions are never moved, killed, or cleaned up
  as part of this change; new dispatches provide a natural migration path.
- `config.py::cfg.tmux_session` owns the fixed workbench session name.
  `data/tmux.py` owns only pure project/window naming and tmux subprocesses, so
  the bottom-of-DAG import rule remains intact.

## Consequences

- All newly dispatched agent windows are visible in one tmux window list, so
  switching projects no longer requires cross-session navigation.
- The shared list can be longer. Project-prefixed window names preserve the
  grouping cue; tmux indices and server-unique window ids preserve exact
  addressing even when display names collide.
- Historical per-project sessions drain only when their processes end. No
  automatic migration or cleanup is introduced.
- This ADR supersedes only ADR-0001's placement rule and ADR-0005's historical
  per-project wording; tmux-first lifecycle, takeover safety, provider routing,
  and Remote Control isolation remain unchanged.
