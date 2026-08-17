# Phone switching uses a scoped second tmux prefix

Status: accepted (2026-08-17).

An in-csctl number key or clickable row is unreachable while Claude, Codex, or
Kimi owns the full-screen pane. Mouse status-bar actions also depend on a phone
terminal translating taps into tmux mouse events. The switch trigger therefore
has to live in tmux, above whichever TUI currently owns input.

## Decision

When csctl creates or reuses its managed `csctl` tmux session, it reads the
effective per-session `prefix2`. If unset (`None`), it sets only that session's
`prefix2` to `C-a`; an existing value is preserved. `Ctrl-A` then `s` reaches
tmux's existing `choose-tree -Zs` binding from any provider TUI. The primary
prefix and global/user tmux configuration are untouched.

The write uses the existing typed tmux subprocess seam. Failure never fails an
already-created agent window, but its diagnostic is retained; no hidden shell
or config-file mutation is added.

## Rejected

- csctl digit shortcuts: useful only while csctl already owns input.
- single-click rows/status bar: phone mouse-event delivery is unproven.
- double-Esc interception: forwarding a lone Esc without breaking provider
  cancel semantics needs a timing state machine and is not fail-safe.
