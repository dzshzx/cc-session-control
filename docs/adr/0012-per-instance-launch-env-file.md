# Per-instance launch-only env_file (spawn secrets without a launcher)

Status: accepted (2026-08-21). Extends ADR-0008: a declared codex identity
can now carry its own launch environment, not just its `CODEX_HOME`.

A declared codex identity backed by an API key (e.g. `~/.codex-eva02`, whose
`config.toml` sets `[model_providers.deepseek] env_key = "DEEPSEEK_API_KEY"`)
only resolves that key from the process environment. The standard way to
supply it is a launcher script — `codex-eva` sources `~/.config/deepseek/env`
then `exec`s codex. But csctl spawns codex itself (ADR-0005: `argv[0]` is the
bare `codex` binary, and `/proc` attribution in ADR-0008 keys on
`basename(argv[0]) == "codex"`, so the launcher cannot be substituted for the
argv). csctl injected only `CODEX_HOME`, so a session started from the Projects
launcher or a resume reached codex with the key absent and codex failed with
`Missing environment variable: DEEPSEEK_API_KEY`.

## The problem: identity env and spawn env are not the same env

`AgentProvider.env` is consumed at two very different boundaries: a real spawn
(execvp, tmux `-e`, the delete subprocess) AND a copied command string the
operator pastes into a shell. A provider API key must reach the first but must
never enter the second — a secret in a clipboard command is both a leak and a
second copy of the key. Folding the key into `env` would have leaked it into
`env_prefix`; keeping the key only in a launcher left csctl's own spawns
without it.

## Decision

Split the two boundaries. `env` stays identity-only (`CODEX_HOME`) and remains
the sole source for the clipboard `env_prefix`. A new `AgentProvider.launch_env`
is the environment for actually spawning the process; it equals `env` for every
provider except a declared codex instance that also declares an `env_file`.

`providers.json` gains an optional per-entry `env_file`:

    {"label": "cx2", "home": "~/.codex-eva02",
     "env_file": "~/.config/deepseek/env"}

`env_file` is an absolute path (`~` expanded), validated like `home` and, like
`home`, not required to exist — an absent file simply yields no extra
environment and codex reports the missing key itself. At each spawn the file is
parsed fresh (so a rotated key needs no restart) as a `KEY=value` shell-`source`
subset: comments and blanks skip, an optional `export ` is dropped, the first
`=` splits, one layer of matching quotes is stripped. No shell is spawned — the
file is read as data, never executed, so values stay literal.

The three spawn sites (`_do_resume_resolved_result` execvp, the tmux resume and
new-session dispatches, the codex delete subprocess) now inject `launch_env`.
`env_prefix` is untouched, so copied commands remain byte-identical and never
carry the secret. The key lives in exactly one file (`~/.config/deepseek/env`),
consistent with the launcher it replaces.

## Rejected

- **A launcher wrapper as `argv[0]`** (e.g. `codex-eva`): breaks the ADR-0008
  `/proc` attribution that identity-matches on `basename(argv[0]) == "codex"`,
  and the copied command would name a non-standard binary.
- **Inline key in `providers.json` or codex `config.toml`
  (`experimental_bearer_token`)**: a second plaintext copy of the secret, and
  the latter is discouraged upstream in favour of `env_key`.
- **Injecting the key into `env`**: leaks it into the clipboard `env_prefix`.
- **Sourcing the file into csctl's own environment at startup**: sprays the
  secret across every session csctl spawns (Claude, Kimi included) instead of
  the one instance that needs it.
