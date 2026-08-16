# cc-session-control

This context defines the operator language for managing agent-CLI sessions
(Claude Code, Codex CLI, Kimi Code) from one local machine, including their
session-level Remote Control exposure.

## Language

**Local Global Workbench**:
A machine-wide management surface for seeing and acting on agent-CLI sessions
across providers and projects. Works tmux-first: its primary verbs dispatch
sessions into project-labelled windows in one shared `csctl` tmux session
(ADR-0001/0006).
_Avoid_: current project view, current session view, Claude-only panel

**Provider**:
The adapter owning ONE agent CLI *identity* inside the workbench (ADR-0005,
ADR-0008): its identity key (`claude` / `codex` / `kimi`, plus
`codex:<label>` for a second declared codex home), typed capabilities (fork,
takeover, liveness grade, cleanup), argv synthesis
(resume / new session / tmux window name), the environment its commands must
carry, and — for non-Claude CLIs — disk session discovery.
`Session.provider` is part of session identity: sids are unique only within a
provider. A capability a provider lacks is refused with a typed reason, never
emulated.
_Avoid_: profile, plugin, treating every CLI as equally deep

**Declared CLI Instance**:
One state home the operator listed in `providers.json`, becoming its own
Provider (ADR-0008). The declaration — not an inherited `CODEX_HOME` — is the
machine's codex inventory: `CODEX_HOME` says which home ONE PROCESS uses and
is inherited by everything a codex session spawns, so reading it as a machine
fact made the workbench's world change with its launch environment. Each
instance carries its home in every command it synthesizes.
_Avoid_: profile (that is codex's own `--profile`, which layers config inside
one home), account, workspace

**Argv-exact Liveness**:
The preferred takeover-grade pid↔session binding for non-Claude providers: a real
resume argv that identifies the session (`codex resume <sid-or-unique-name>`,
`kimi --session <sid>`). Unknown or ambiguous Codex names, bare pickers,
launcher-created NEW sessions, bare-launched TUIs, and CLI daemons remain
unbound and are never stop/takeover targets — unless the kimi runtime
registry (below) proves them.
_Avoid_: cwd-guessing as liveness, pane-text busy regexes

**Runtime-registry Liveness** (kimi, opt-in):
The strongest non-Claude binding when configured: kimi's official
SessionStart/SessionHeartbeat/SessionEnd hooks run `csctl _kimi-hook`,
which maintains
`~/.kimi-code/run/<pid>.json` (`sessionId` + `procStart`) — the CLI's own
self-report, the same shape as Claude's `sessions/<pid>.json`. csctl
re-verifies pid identity and start time per entry, so stale, forged, or
disputed entries never bind. Covers every kimi session regardless of launch
surface, bare-launched TUIs included; the hook fires when a session
materializes — at once for `--prompt` and for a `--session` resume, and at
the first prompt for a NEW session, whose sid does not exist before then.
SessionEnd is unreliable
(0.35.0 leaves entries behind, so `prune_gone_entries` bounds the
directory), and SessionStart delivery itself is fire-and-forget: a start
that never lands is invisible to the registry by construction (seen on
0.35.0 and 0.36.1), leaving `run/hook-errors.log` unable to show it.
The SessionHeartbeat rule (0.36.1+) is the self-heal: kimi re-fires every
60 s while the session lives, so a missed start becomes a ≤60 s unbound
window. Hooks are startup config — a session launched before the rule was
added stays unbound until it is reopened.
_Avoid_: treating the registry file alone as proof without the /proc recheck;
inferring a binding for an unbound live process from its directory

**Dispatch-metadata Liveness**:
The supplementary non-Claude binding for sessions csctl dispatched into tmux.
Kimi makes this source essential by rewriting away its resume argv (cmdline
collapses to `kimi-code` or bare `kimi` — both shapes live within one
version, picked by launch path). csctl joins its own
`@csctl_sid`/`@csctl_provider` window options to one identity-checked pane TUI
process. Missing, incomplete, mismatched, or ambiguous evidence binds nothing;
window names never participate, and bare TUIs stay unbound.
_Avoid_: treating tmux presence or a window name alone as session identity

**Session** (formerly "Claude Code Session"):
A resumable agent-CLI conversation or execution context whose state may be
visible through the owning CLI's on-disk records, agent listings, or Remote
Control exposure. The session is the durable record; agent records and
runtimes are ways that record is or was being executed. Rich
liveness/registry semantics below (busy/idle status, bridge) are
Claude-specific; non-Claude sessions carry only the conservative argv-exact
and dispatch-metadata subset above.
_Avoid_: chat, transcript file

**Agent**:
A Claude Code execution entry listed by `claude agents --json`; csctl reads it
as liveness evidence (the 来源 `BG` badge) but no longer manages its
lifecycle (ADR-0009).
_Avoid_: process, task

**tmux Residency (tmux 驻留)**:
The property of a live session whose process runs inside a tmux pane; a
resident session survives terminal and network disconnects. The primary
protection csctl works toward.
_Avoid_: detached, daemonized, "in tmux" without saying resident

**Workbench tmux Session**:
The single tmux session named `csctl` into which the workbench dispatches new,
resumed, forked, and backgrounded agent sessions. Project identity remains the
absolute cwd; the project basename is display-only metadata in the window
name. Existing resident windows in any tmux session are entered in place
rather than migrated.
_Avoid_: one tmux session per project, treating a window name as identity

**tmux Resume (tmux 接回)**:
Resuming a session inside its project-labelled window in the workbench tmux
session and bringing the operator's terminal into that window — the primary
resume verb; makes the session tmux-resident. A session already resident in
any tmux session is entered in place.
_Avoid_: attach

**Terminal Resume (终端接回)**:
Resuming a session in the bare terminal by replacing the csctl process; the
session dies with the terminal. The fallback when tmux is unavailable or
unwanted.
_Avoid_: unqualified "resume/接回"

**Backgrounding (转后台)**:
Moving a session into its project-labelled window in the workbench tmux
session without entering it and without enabling Remote Control; the operator
stays in csctl.
_Avoid_: relaunch, RC relaunch (the pre-0.7 behavior that also minted a cloud
environment)

**Project**:
An absolute directory path carrying a provenance evidence set — the
membership unit of the Projects tab (ADR-0007). Membership is the union of
three evidence tiers, minus hygiene rules, with operator curation on top:
**Pinned** (operator-curated in the curation store; immune to hygiene and
decay), **Trusted** (a provider's trust store covers the directory — Claude
effective trust with ancestor inheritance, codex/kimi exact-match records),
**Observed** (any provider has session activity in the directory within the
last 30 days). Temp roots and missing directories are hygiene-excluded
unless the entry is pinned; a hidden entry is suppressed regardless of
evidence. Trust inheritance only ever *qualifies* a
recorded candidate — it never *generates* one, so a trusted `/` cannot flood
the tab. The absolute directory path is the project's identity everywhere
(tmux window metadata, claude.json lookups); the display name is a derived
basename. Claude effective trust keeps its ADR-0003 semantics (`hasTrustDialogAccepted: true`
on the entry or any ancestor, explicit-False-is-not-a-veto, normpath never
realpath, last semantically verified on Claude Code 2.1.218, 2026-07-23) and
remains an upstream-dependent contract: each release must rerun
`docs/claude-code-compatibility.md` and record any unverified item. Platform
temp directories (`tempfile.gettempdir()`, `/tmp`, `/var/tmp`, and anything
beneath them) stay working space, not projects: discovery never lists them —
the trust state itself stays untouched, so a deliberately trusted `/tmp`
keeps suppressing dialogs for scratch sessions — while pinned entries stay
listed.
_Avoid_: workspace-relative short names as identity, reading the raw
`hasTrustDialogAccepted` flag as the trust set, assuming a workspace root,
treating a trusted temp root as a project

**Membership Evidence (成员证据)**:
The per-project record of WHY a directory is on the Projects tab:
`trusted_by` (provider keys whose trust store covers it — claude via
effective/inherited trust, codex/kimi via exact-match records) and
`observed_by` (provider keys with session activity there), plus the
`pinned`/`hidden` curation flags. Carried on the row model; the tab surfaces
`pinned` via ordering and `hidden` via the status-bar count. No badge column
(removed with the RC/agents surfaces, ADR-0009).
_Avoid_: a single is-a-project boolean, deriving membership from one CLI's
records only

**Curation Store (取舍存储)**:
The one csctl-OWNED membership source (`cfg.curation_file`, XDG config
home): the operator's `pinned` and `hidden` directory lists, mutually
exclusive (pinning unhides, hiding unpins). Read on every refresh; written
only by the Projects tab's `p`/`h` verbs through advisory-locked atomic
replace. Every other membership source (the CLI trust stores) is read-only
for csctl.
_Avoid_: writing operator intent into claude.json or any provider's files

**Remote Control** (umbrella term — two distinct concepts, do not conflate):

**Session Remote Control** (secondary control surface — demoted from primary
by ADR-0001; tmux Residency is the anti-disconnect mechanism, RC is for
phone/web control):
Exposing one local Claude Code session to the Claude mobile app / claude.ai/code
so it can be driven from outside the terminal. Observable on the local machine
when `~/.claude/sessions/<pid>.json` carries a `bridgeSessionId` in the
`session_*` namespace. Enabled via `claude --remote-control [name]`, the
in-session `/remote-control` command, or `remoteControlAtStartup`.
_Avoid_: confusing it with a project RC server (an upstream concept csctl no
longer models); tmux window.

**Bridge Environment**:
The cloud-side linkage that backs remote control. The namespace csctl reads is
`session_*` (in `sessions/*.json`, session remote control). The other upstream
namespaces (`cse_*` in `jobs/*/state.json`, `env_*` on a project RC server's
stdout) exist in Claude Code, but csctl no longer reads either — it models
neither background agents nor project RC servers (ADR-0009). The **suffix is
the canonical environment id _within_ a namespace**.

Lifecycle (verified on this machine): enabling RC on a session **mints a new**
environment id; disabling sets `bridgeSessionId` to `null` (a **transient** state
— observed on disconnect, then overwritten by a fresh id on the next enable, so a
random snapshot usually shows only absent-or-string); re-enabling mints
**another** new id. `sessions/<pid>.json` keeps only the *current* binding
(single field, overwritten), so toggled-away environments vanish from structured
state and survive only as noisy mentions in transcripts. Consequences:
- csctl can reliably surface **currently bound** environments (bridge truthy
  AND the owning pid alive, verified by `procStart`) — the session RC badge.
- Toggled-away / historical environments leave **no structured trace** on disk
  (no `null`/history), so csctl does not track them.
- Claude Code exposes **no local command to deregister** a cloud / mobile entry;
  deletion stays manual on claude.ai/code.
_Avoid_: claiming csctl can delete a cloud environment, or that file presence /
a non-null `bridgeSessionId` alone proves a session is currently exposed.

**Live Session**:
A Claude Code session or agent that currently has an active local runtime and
can be unsafe to delete without first stopping or detaching it.
_Avoid_: existing transcript, recent session
