# cc-session-control

This context defines the operator language for managing agent-CLI sessions
(Claude Code, Codex CLI, Kimi Code, opencode) from one local machine, including their
session-level Remote Control exposure.

## Language

**Local Global Workbench**:
A machine-wide management surface for seeing and acting on agent-CLI sessions
across providers and projects. Works tmux-first: its primary verbs dispatch
sessions into CLI-named windows in one shared `csctl` tmux session
(ADR-0001/0006).
_Avoid_: current project view, current session view, Claude-only panel

**Provider**:
The adapter owning ONE agent CLI *identity* inside the workbench (ADR-0005,
ADR-0008): its identity key (`claude` / `codex` / `kimi` / `opencode`, plus
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
Provider (ADR-0008). The declaration — not an inherited `CODEX_HOME`, which
describes one process's launch environment rather than the machine — is the
codex inventory; each instance carries its home in every command it
synthesizes.
_Avoid_: profile (that is codex's own `--profile`, which layers config inside
one home), account, workspace

**Argv-exact Liveness**:
The preferred takeover-grade pid↔session binding for non-Claude providers: a real
resume argv that identifies the session (`codex resume <sid-or-unique-name>`,
`kimi --session <sid>`, `opencode --session <sid>`). Anything else — unknown or
ambiguous Codex names, bare pickers, launcher-created NEW sessions,
bare-launched TUIs, CLI daemons — stays unbound and is never a stop/takeover
target unless the kimi runtime registry (below) proves it.
_Avoid_: cwd-guessing as liveness, pane-text busy regexes

**Runtime-registry Liveness** (kimi, opt-in):
The strongest non-Claude binding when configured: kimi's official session
hooks run `csctl _kimi-hook`, which keeps a per-pid self-report (`sessionId`
+ `procStart`) that csctl re-verifies against `/proc` before binding — so it
covers every kimi session, bare-launched TUIs included, from the moment its
sid exists. Hook events, delivery bounds, and versioned evidence: ADR-0005
(2026-08-12/13/16 amendments) and `docs/claude-code-compatibility.md`.
_Avoid_: treating the registry file alone as proof without the /proc recheck;
inferring a binding for an unbound live process from its directory

**Dispatch-metadata Liveness**:
The supplementary non-Claude binding for sessions csctl dispatched into tmux:
csctl joins its own `@csctl_sid`/`@csctl_provider` window options to one
identity-checked pane TUI process. Essential for kimi, whose runtime rewrites
away its resume argv (ADR-0005 C1). Missing, incomplete, mismatched, or
ambiguous evidence binds nothing; window names never participate, and bare
TUIs stay unbound.
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
absolute cwd; a window is named only by the bare CLI it runs (`claude`,
`codex`, `kimi`, `opencode`, or a declared identity's tag) and names are
display-only. Existing resident windows in any tmux session are entered in
place rather than migrated.
_Avoid_: one tmux session per project, treating a window name as identity,
project- or sid-labelled window names

**Mobile Switch Prefix (手机切换前缀)**:
The managed `csctl` tmux session's second prefix. When its effective `prefix2`
is `None`, csctl scopes `C-a` to that session; `C-a s` reaches tmux
`choose-tree -Zs` even while a provider TUI owns pane input. Existing prefix2,
the primary prefix, and global tmux configuration are preserved.
_Avoid_: an in-csctl shortcut claimed to work from a provider TUI; global bind

**Hosted Session (托管会话)**:
A Codex active rollout whose exact path is open in the owning app-server fd
table. It is present in Desktop/IDE but has no session-owning pid: `hosted` is
independent of `alive`, read-only, and never authorizes resume, fork, stop, or
delete. Source badges alone do not prove hosting.
_Avoid_: using the shared app-server pid as a session pid; hosted = alive

**tmux Resume (tmux 接回)**:
Resuming a session inside a CLI-named window in the workbench tmux
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
Moving a session into a CLI-named window in the workbench tmux
session without entering it and without enabling Remote Control; the operator
stays in csctl.
_Avoid_: relaunch, RC relaunch (the pre-0.7 behavior that also minted a cloud
environment)

**Project**:
An absolute directory path carrying a provenance evidence set — the
membership unit of the Projects tab (ADR-0007): **Pinned** (operator-curated)
∪ **Trusted** (a provider's trust store covers it) ∪ **Observed** (recent
session activity), minus hygiene rules (temp roots, missing directories), with
operator curation on top. The absolute path is the project's identity
everywhere; the display name is a derived basename. Tier rules, decay, and
Claude's effective trust: ADR-0007 / ADR-0003 + `docs/claude-code-compatibility.md`.
_Avoid_: workspace-relative short names as identity, reading the raw
`hasTrustDialogAccepted` flag as the trust set, assuming a workspace root,
treating a trusted temp root as a project (hygiene hides it; its trust state
stays untouched so scratch sessions under `/tmp` still skip the dialog)

**Membership Evidence (成员证据)**:
The per-project record of WHY a directory is on the Projects tab:
`trusted_by` (provider keys whose trust store covers it) and `observed_by`
(provider keys with session activity there), plus the `pinned`/`hidden`
curation flags. Carried on the row model; the tab surfaces `pinned` via
ordering and `hidden` via the status-bar count — there is no badge column
(ADR-0009).
_Avoid_: a single is-a-project boolean, deriving membership from one CLI's
records only

**Curation Store (取舍存储)**:
The one csctl-OWNED membership source (`cfg.curation_file`, XDG config
home): the operator's `pinned` and `hidden` directory lists, mutually
exclusive (pinning unhides, hiding unpins). Written only by the Projects tab's
`p`/`h` verbs; every other membership source (the CLI trust stores) is
read-only for csctl.
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
The cloud-side linkage behind remote control; the suffix of a namespaced id is
the canonical environment id within its namespace. csctl reads only the
`session_*` namespace (`bridgeSessionId` in `sessions/<pid>.json` — the
current binding only, so no history; Claude Code has no local deregister); the
upstream `cse_*`/`env_*` namespaces are no longer read (ADR-0009). Verified
enable/disable lifecycle: `docs/claude-code-compatibility.md`.
_Avoid_: claiming csctl can delete a cloud environment, or that file presence /
a non-null `bridgeSessionId` alone proves a session is currently exposed.

**Live Session**:
A Claude Code session or agent that currently has an active local runtime and
can be unsafe to delete without first stopping or detaching it.
_Avoid_: existing transcript, recent session
