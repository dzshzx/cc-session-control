# Remove Remote Control management and background-agent management

Status: accepted (2026-08-13). Supersedes the corresponding clauses of
ADR-0001 (Remote Control stays "demoted, not removed"; the 后台 tab and
项目 → 会话 → 后台 tab order), ADR-0004 (the headless CLI is
`resume` + `agents`; the Projects tab is the sole RC surface; the
per-project `remoteControlAtStartup` flag is "unaffected"), and ADR-0007
(hygiene waives entries holding an rc window; provenance renders as
钉/隐/信cc/信cx/信km/活… badges).

Since ADR-0001 demoted Remote Control to a secondary surface, csctl kept
two management surfaces for operators it does not have: project RC-server
management (start/stop/auto-start tri-state, server discovery) and Claude
background-agent management (list/take over/respawn/watch/stop/remove).
Neither surface saw use on the deployment machine, and both duplicated what
their CLIs already do natively — `/remote-control` inside a session covers
phone/web control, and `claude`/`claude agents` covers background agents.

## Decision

- **Remove Remote Control management entirely.** The Projects tab loses its
  状态/自动远控/启动模式 columns, the `o`/`s`/`c`/`S` server verbs, the
  RC-server listing, and the whole RC data/action layer (`data/rc.py`, RC
  outcomes, `claude remote-control` process discovery, the `rc` tmux
  session, `CSCTL_RC_SESSION`). The tab becomes a pure launcher plus
  membership curation: 项目/目录 columns, Enter CLI chooser, `x`/`k`
  direct launches, and the `p`/`h`/`H` curation verbs.
- **Remove background-agent management entirely.** The 后台 tab, the
  `csctl agents` command, and the `jobs/<short>/state.json` registry scan
  go away; csctl no longer lists, takes over, respawns, watches, stops, or
  removes Claude Code background agents. `claude agents --json` stays in as
  a liveness evidence source (`alive_map`), and session cleanup still
  removes a dead session's `jobs/<sid-prefix>` artifacts by its existing
  anchors.
- **Remove the 证据 provenance-badge column.** Provenance stays on the row
  model (`trusted_by`/`observed_by`/`pinned`/`hidden`) for ordering
  (pinned first) and status-bar counts; the badge column goes with the RC
  surface.
- **Session-level remote-control exposure is unaffected.** The 📱 badge
  (`Session.rc_exposed` → `liveness.is_rc_exposed`) and the in-session
  `/remote-control` escape hatch remain, because they are properties of a
  session, not a managed service.
- **Hygiene waiver narrows.** ADR-0007's "entries holding an rc window"
  exemption dies with the rc window concept; only pinned entries stay
  exempt from temp-root/missing-directory hygiene.

## Consequences

- The TUI has two tabs (项目 / 会话); the headless CLI is `resume` only.
- External consumers of `csctl agents` (e.g. the claude-session-doctor
  skill) must drop that dependency; `csctl resume` is the remaining
  headless surface.
- The knowledge docs (CLAUDE.md, CONTEXT.md, AGENTS.md, README) are synced
  to the removal in the same change.
