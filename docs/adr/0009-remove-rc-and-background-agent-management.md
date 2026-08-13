# Remove Remote Control management and background-agent management

Status: accepted (2026-08-13).

This ADR supersedes only the following management clauses; the remaining
decisions in each ADR stay in force:

- **ADR-0001:** the 后台 tab and 项目 → 会话 → 后台 tab order, project-tab
  `o`/`c` Remote Control management, and the conclusion that Remote Control
  is "demoted, not removed" as a csctl-managed surface. The tmux-first
  lifecycle and session-level `/remote-control` escape remain.
- **ADR-0002:** `AgentJob` as a frozen view/action mutation model. Atomic
  refresh generations, single-flight actions, and main-loop-only widget
  mutation remain.
- **ADR-0003:** the RC startup trust gate, `remoteControlAtStartup` writes,
  and RC-specific lifecycle diagnostics. Typed `~/.claude.json` reads,
  effective-trust semantics, fail-closed evidence, and cleanup diagnostics
  remain.
- **ADR-0004:** the `resume` + `agents` headless CLI, the Projects tab as the
  sole RC surface, and the claim that per-project `remoteControlAtStartup`
  is unaffected. Its other surface-reduction and cleanup decisions remain.
- **ADR-0005:** provider capabilities and UI consequences for background
  agents and RC, including Claude-only Agents and Projects-RC surfaces. The
  provider session layer, conservative liveness, multi-CLI launcher, and
  Claude-only cleanup boundary remain.
- **ADR-0006:** background-agent respawn/window placement and the separate
  managed `rc` tmux session, including `CSCTL_RC_SESSION`. The unified
  `csctl` session and in-place entry for legacy/user tmux residency remain.
- **ADR-0007:** the rc-window hygiene waiver, RC-start-gate consequences,
  provenance badge rendering, and the promise that rc-window evidence keeps
  a row visible. Evidence-tier membership and provenance on the model remain.

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
- The claude-session-doctor skill, the documented consumer of
  `csctl agents`, is deprecated rather than updated; session rescue stays
  in csctl itself (`csctl resume` and the TUI).
- The knowledge docs (CLAUDE.md, CONTEXT.md, AGENTS.md, README) are synced
  to the removal in the same change.
