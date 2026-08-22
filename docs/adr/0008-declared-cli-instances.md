# Operator-declared CLI instances (multi-home codex)

Status: accepted (2026-08-13). Extends the ADR-0005 provider layer from
"one provider per CLI" to "one provider per CLI *identity*".
The 证据-column clause in Consequences ("widened 20→24") is superseded by ADR-0009, which
removed the badge column the same day; `trusted_by`/`observed_by` stay on the row model.
Read-only re-check 2026-08-22: codex 0.149.0 still exposes no `--codex-home`; the `CODEX_HOME`-only
premise holds.
Extended by ADR-0012 (a declared identity may carry a launch-only `env_file`).

Codex supports a second identity purely through its official relocation
variable: `CODEX_HOME=<path> codex …` (there is no `--codex-home` flag;
`--profile` layers config within one home, verified against
`codex --help`, 0.147.0). A launcher script that exports the variable and
`exec`s codex is the standard shape — on this machine `codex-eva` points at
`~/.codex-eva02`, a distinct DeepSeek-backed identity with its own
sessions, its own `config.toml [projects.*]` trust table, and its own
`session_index.jsonl`.

csctl could not see any of it, and the reason exposes a deeper modelling
error than "one home was hardcoded".

## The problem: a per-process variable read as a machine fact

`cfg.codex_home` was `os.environ.get("CODEX_HOME") or ~/.codex`. But
`CODEX_HOME` answers **"which home does THIS process use"** — a per-process
runtime parameter — while csctl, a machine-level operator workbench, needs
**"which homes exist on this machine"**. Reading the former as the latter
makes the workbench's world depend on who launched it.

That is not theoretical. The variable is inherited by everything a codex
session spawns, csctl included. Measured before the change:

```
csctl resume                      → [codex] 019ff558…  (default home)
CODEX_HOME=~/.codex-eva02 csctl   → [codex] 019ff6b5…  (eva only)
```

In the second run the default home's 384 sessions were simply absent — and
running csctl from inside a `codex-eva` session produces exactly that
environment. A second, quieter bug rode along: the copied resume command
was a bare `codex resume <eva-sid>`, which fails in any shell that has not
inherited the same identity.

## Decision

**The operator declares this machine's codex state homes; `CODEX_HOME`
stops deciding the inventory.**

`cfg.provider_config_file` (`~/.config/csctl/providers.json`, XDG-respecting,
read by `data/provider_config.py`) may declare `codex_homes`:

```json
{
  "codex_homes": [
    {"label": "cx",  "home": "~/.codex"},
    {"label": "cx2", "home": "~/.codex-eva02"}
  ]
}
```

- The list is the **complete** codex inventory. `CODEX_HOME` then plays no
  part, so the same machine state is shown no matter which identity's
  session csctl was launched from.
- **Absent file = today's behavior, exactly**: one instance following
  `cfg.codex_home`, empty `env`, byte-identical synthesized commands. A
  present-but-broken file degrades to that same single instance and reports
  why (`providers.config_issues()` merges into the scan issue stream) —
  never an emptied codex view.
- The **first** entry keeps provider key `codex`; later entries get
  `codex:<label>`. Existing `Session.provider` values, dispatched windows'
  `@csctl_provider` metadata, and `CSCTL_PROVIDERS=…,codex,…` (allow-listing
  is by base key) therefore need no migration.
- `label` is the CLI-column tag (ASCII alphanumeric, ≤3 chars — the column
  is 3 cells wide) and prefixes per-session tmux window names.

### Every command states its identity

`AgentProvider.env` is the environment a provider's commands must carry:
empty for single-home CLIs, `{"CODEX_HOME": <home>}` for a declared codex
identity. Each boundary injects it in its own idiom, never by string
-concatenating a shell command: tmux spawns pass `new-window -e KEY=VALUE`
(argv, and set on the *window* so a later manual re-run in that window keeps
the identity), `execvp` resumes update `os.environ` first, copied commands
get a `shlex.quote`d leading assignment, and `codex delete` passes an env
mapping to `subprocess.run`.

`window_tag` exists because `:` is tmux target syntax (`session:window`) —
a multi-instance key must never reach a window name.

### Attributing running processes

Two identities run the same binary with identical argv (the launcher
`exec`s codex, so argv0 is plain `codex`; verified `exec -a` *can* relabel
it, but csctl must not depend on an operator's script). The only evidence is
the process's own `CODEX_HOME`, so the `/proc` walk captures a **whitelisted**
environ key per matched process (`ProcCli.env`; `/proc/<pid>/environ`
carries secrets wholesale, so only requested keys are retained, and an
unreadable block yields `None`, distinct from `{}`).

This attribution is deliberately scoped to the **unbound-live hint** only:

- **Liveness is not filtered by it.** argv and dispatch-metadata bindings —
  the kill targets — are already identity-safe: a sid resolves only against
  the home whose rollout tree records it, and metadata carries the instance
  key. Filtering them on environ would DROP real bindings whenever the
  environ block is unreadable.
- **Hints are filtered by it**, so one identity's bare TUI stops marking the
  other's rows "possibly held".
- **No evidence (`env is None`) warns on every identity.** A hint is a
  warning: a redundant one costs one extra confirmation, a missing one loses
  a double-open warning, so absent evidence must fail toward warning.

## Consequences

- Membership (ADR-0007) gains one provenance source per identity for free —
  each declared home's trust table and session activity contribute their own
  `信cx`/`活cx2` badges (the 证据 column widened 20→24 accordingly).
- The registry is now built once per process and cached (`providers.reset()`
  drops it); changing `providers.json` needs a csctl restart, like every
  other `CSCTL_*` setting. The test suite resets it per test and points XDG
  at a tmp dir, so a dev box's real declaration cannot leak into tests.
- Sessions the operator holds under an identity whose home is *not* declared
  become invisible — the cost of making the inventory explicit. It is a
  visible, self-inflicted omission, unlike the silent drift it replaces.
- Nothing here is codex-specific by construction, but only codex is wired:
  kimi and Claude declare one home each (`env_keys = frozenset()`, `env =
  {}`). A future multi-home kimi would reuse the same seam.

## Rejected alternatives

- **An env var (`CSCTL_CODEX_HOMES=…`) instead of a file.** It would hang
  the *composition of the world* on the very mechanism that drifts, and it
  would have to be reconciled against `CODEX_HOME` (same home declared
  twice → one identity listed twice). Rejected despite matching the existing
  `CSCTL_*` style: those settings are behavior switches, where drift costs a
  color scheme, not a missing data view.
- **Auto-discovering `~/.codex-*` siblings.** This machine also holds
  `~/.codex-tmp` and `~/.codexcont-backup`; guessing would list junk as
  identities. Declaration is explicit by design.
- **Requiring the `codex-eva` launcher and matching on argv0.** Depends on
  an operator script, misses `CODEX_HOME=… codex` invocations, and is not an
  official upstream contract.
