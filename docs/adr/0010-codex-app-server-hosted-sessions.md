# Codex app-server sessions are hosted and read-only

Status: accepted (2026-08-17).

Codex Desktop/IDE app-server processes keep rollout files open while they host
threads. Their pid is shared by many threads, so treating it as a session pid
would create a catastrophic multi-session kill target; treating every held
thread as dead invites duplicate resumes.

## Decision

- `data/proc.py` remains the only `/proc` seam and returns typed open-file
  inventory. Vanished processes/fds are normal races; unreadable inventory is
  an issue, not an empty success.
- The Codex provider accepts only exact `.jsonl` targets beneath that identity's
  active `sessions/` root, held by a process whose argv is exactly Codex
  `app-server` and whose `CODEX_HOME` belongs to the identity when readable.
- `Session.hosted` is independent of `alive`. It carries no pid, procStart,
  currentness, tmux residency, or destructive authority.
- TUI/headless output says hosted/read-only and emits no command. Resume,
  background, fork, stop, copy-command, and delete paths refuse it; Codex
  execution and TUI command-copy paths recheck hosted evidence so a stale dead
  row cannot race into a duplicate resume.

## Consequences

An app-server fd failure degrades Codex discovery visibly. This is deliberate:
unknown hosting evidence must not become permission to resume or delete.
Desktop/IDE source badges remain launch-origin metadata and do not themselves
prove hosting.
