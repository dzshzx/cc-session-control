# tmux-first session dispatch

Status: accepted (2026-07-06); placement rule superseded by ADR-0006;
project-RC and background-tab clauses superseded by ADR-0009
Projects-tab `Enter` = new *claude* session clause superseded by ADR-0005 (Enter opens the
provider chooser; claude is the default focus); hosted Codex rows are exempt from the
Sessions verbs per ADR-0010.
Extended by ADR-0004 (Remote Control demoted to a secondary surface; tmux residency is the
anti-disconnect mechanism).

csctl's core operator need is that Claude Code sessions survive terminal /
network disconnects (flaky SSH, phone connections). Two mechanisms could carry
that: per-session Remote Control (cloud bridge) or tmux residency. We chose
**tmux as the primary mechanism** and repositioned csctl as a tmux-first
dispatch center: every primary verb puts the operator into — or the session
into — a per-project tmux window.

ADR-0006 retains this tmux-first lifecycle decision but replaces the original
per-project tmux-session placement with one shared interactive `csctl` session.
The per-project wording below records the decision as originally accepted.

## Decision

- **Sessions / 后台 tab `Enter` = tmux 接回** (was: bare-terminal resume).
  Bare-terminal resume is demoted to `t` (终端接回) as the fallback for
  environments without tmux.
- **项目 tab `Enter` = 新建 tmux claude 会话并进入** (was: start the project RC
  server, demoted to `o`). Tab order becomes 项目 → 会话 → 后台 with startup on
  项目 — the launcher-first mental model.
- **Sessions `R` = 转后台 without Remote Control** (migrate a bare session into
  tmux, stay in csctl). The RC-relaunch action is **removed** from the Sessions
  tab; `f` 分叉 and 后台 接回 also go through tmux.
- Live sessions show a **tmux-residency badge (⧉)** so unprotected bare-terminal
  sessions are visible at a glance.

## Why tmux over Remote Control

Every fresh `--remote-control` process **mints a new cloud environment id**, and
Claude Code has no local deregister — routine RC use piles up duplicate
mobile/web environment entries that must be deleted by hand on claude.ai/code.
tmux gives the same disconnect-survivability with zero cloud side effects.
Remote Control stays available as the *secondary* phone/web control surface
(项目 tab `o` start / `c` remoteControlAtStartup, or in-session
`/remote-control`) — demoted, not removed.

## Consequences

- `t` no longer means "tmux" anywhere; it means **T**erminal resume. `o` means
  RC start and exists only on the 项目 tab (the 后台 tab's old `o` alias is
  dropped).
- Muscle memory from ≤0.6.x: `Enter` now lands in tmux instead of the bare
  terminal — deliberate; the anti-disconnect path must be the default.
- The Sessions tab can no longer mint cloud environments at all.
