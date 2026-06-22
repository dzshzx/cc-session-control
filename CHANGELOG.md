# Changelog

## 0.1.0 (2026-06-22)

Initial release.

- Sessions Tab: view, resume, terminate, delete Claude Code sessions
- Remote Control Tab: start/stop RC servers, toggle auto-start, crash recovery
- Cleanup Tab: prune empty/short sessions, sweep orphan directories
- CLI subcommands: `csctl rc`, `csctl prune`
- Cross-platform clipboard support (WSL/macOS/Wayland/X11)
- Auto-refresh every 10 seconds in TUI
