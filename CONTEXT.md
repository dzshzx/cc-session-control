# cc-session-control

This context defines the operator language for managing Claude Code sessions,
agents, and Remote Control environments from one local machine.

## Language

**Local Global Workbench**:
A machine-wide management surface for seeing and acting on Claude Code sessions,
agents, and Remote Control environments across projects. Works tmux-first: its
primary verbs dispatch sessions into per-project tmux windows (ADR-0001).
_Avoid_: current project view, current session view

**Claude Code Session**:
A resumable Claude Code conversation or execution context whose state may be
visible through transcripts, agent listings, Remote Control, or background
execution surfaces. The session is the durable record; agents and runtimes are
ways that record is or was being executed.
_Avoid_: chat, transcript file

**Agent**:
A Claude Code execution entry that may run interactively, in the background, or
under a managed lifecycle separate from a plain terminal session. An agent is a
runtime or lifecycle wrapper for a Claude Code session, not a separate durable
work unit.
_Avoid_: process, task

**tmux Residency (tmux 驻留)**:
The property of a live session whose process runs inside a tmux pane; a
resident session survives terminal and network disconnects. The primary
protection csctl works toward.
_Avoid_: detached, daemonized, "in tmux" without saying resident

**tmux Resume (tmux 接回)**:
Resuming a session inside its per-project tmux window and bringing the
operator's terminal into that window — the primary resume verb; makes the
session tmux-resident. A session already resident is entered in place.
_Avoid_: attach

**Terminal Resume (终端接回)**:
Resuming a session in the bare terminal by replacing the csctl process; the
session dies with the terminal. The fallback when tmux is unavailable or
unwanted.
_Avoid_: unqualified "resume/接回"

**Backgrounding (转后台)**:
Moving a session into its per-project tmux window without entering it and
without enabling Remote Control; the operator stays in csctl.
_Avoid_: relaunch, RC relaunch (the pre-0.7 behavior that also minted a cloud
environment)

**Project**:
A directory recorded in `~/.claude.json`'s `projects` map that is *effectively
trusted*: its own entry or ANY ancestor entry carries
`hasTrustDialogAccepted: true`. This mirrors claude's runtime trust-dialog
gate, which inherits trust down the directory tree (verified on claude
2.1.218). The absolute directory path is the project's identity everywhere
(rc-enabled list, tmux window metadata, claude.json lookups); the display name
is a derived basename. An entry with an explicit False flag under a trusted
ancestor IS a project — that footprint means "dialog suppressed, never asked",
not "declined" (declining writes no entry at all).
_Avoid_: workspace-relative short names as identity, reading the raw
`hasTrustDialogAccepted` flag as the trust set, assuming a workspace root

**Remote Control** (umbrella term — two distinct concepts, do not conflate):

**Session Remote Control** (secondary control surface — demoted from primary
by ADR-0001; tmux Residency is the anti-disconnect mechanism, RC is for
phone/web control):
Exposing one local Claude Code session to the Claude mobile app / claude.ai/code
so it can be driven from outside the terminal. Observable on the local machine
when `~/.claude/sessions/<pid>.json` carries a `bridgeSessionId` in the
`session_*` namespace. Enabled via `claude --remote-control [name]`, the
in-session `/remote-control` command, or `remoteControlAtStartup`.
_Avoid_: confusing it with the project RC server; tmux window.

**Project RC Server** (secondary concept):
A persistent `claude remote-control --name <name>` process that accepts multiple
phone/web sessions for one directory. csctl currently models it as a tmux
window — this is the only Remote Control concept csctl models today.

Observability (verified): the server leaves **zero footprint** in `sessions/`,
`jobs/`, or `claude agents --json`; its only reliable local signal is the
`claude remote-control --name <name>` **process** itself, and its cloud env id
(`env_*`) appears only on the server's stdout / QR. A server launched outside
csctl's tmux is therefore invisible unless csctl scans `/proc` for the process.
Verified via a live probe: the **server's** `/proc/<pid>/cmdline` shows the full
`claude remote-control --name <name> --spawn <mode>` argv (a bare *interactive*
`claude` instead collapses its cmdline to just `claude`), so match on the
**cmdline argv** (program basename `claude` + `remote-control` + `--name`), not on
`comm` alone — and exclude other tools, e.g. codex also runs `--remote-control`
(as a flag), filtered out by cmdline.
_Avoid_: equating it with session remote control.

**Bridge Environment**:
The cloud-side linkage that backs remote control. Three observable prefixes,
each tied to a different RC concept: `session_*` (in `sessions/*.json`, session
remote control), `cse_*` (in `jobs/*/state.json`, background agents), and
`env_*` (project RC server — appears only on the server's stdout / QR, in **no**
state file). The **suffix is the canonical environment id _within_ a namespace** —
within `cse_*`, a resume pair shares one env (e.g. two jobs binding the same
`cse_…`). Cross-namespace linking (`session_*` ↔ `cse_*` by suffix) does **not**
work: each RC-enable mints a unique suffix, so a session-RC env and a
background-agent env never share one (verified: zero overlap). Dedup is
within-namespace, not cross-view.

Lifecycle (verified on this machine): enabling RC on a session **mints a new**
environment id; disabling sets `bridgeSessionId` to `null` (a **transient** state
— observed on disconnect, then overwritten by a fresh id on the next enable, so a
random snapshot usually shows only absent-or-string); re-enabling mints
**another** new id. `sessions/<pid>.json` keeps only the *current* binding
(single field, overwritten), so toggled-away environments vanish from structured
state and survive only as noisy mentions in transcripts. Consequences:
- csctl can reliably enumerate **currently bound** environments (bridge truthy
  AND the owning pid alive, verified by `procStart`) — the alive-gated `observe_live`.
- To make toggled-away / orphaned environments traceable, csctl maintains its own
  **append-only ledger** (`$XDG_CONFIG_HOME/csctl/environments.jsonl`): every
  refresh records the **file-referenced** set (any env a `sessions/*.json` /
  `jobs/*/state.json` / running RC server references now, alive or not), so an env
  that later drops out of that set surfaces as an **orphan = ledger − file-referenced**
  (the manual-delete checklist). The ledger still **cannot back-fill** environments
  minted while csctl was not running (no `null`/history on disk), so the orphan
  list is inherently incomplete.
- Claude Code exposes **no local command to deregister** a cloud / mobile entry;
  deletion stays manual on claude.ai/code.
_Avoid_: claiming csctl can delete a cloud environment, or that file presence /
a non-null `bridgeSessionId` alone proves a session is currently exposed.

**Live Session**:
A Claude Code session or agent that currently has an active local runtime and
can be unsafe to delete without first stopping or detaching it.
_Avoid_: existing transcript, recent session
