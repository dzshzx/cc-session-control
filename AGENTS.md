# Agent Rules

Full development guide (architecture, commands, invariants) lives in `CLAUDE.md` — read @CLAUDE.md before starting any task; `CONTEXT.md` is the domain glossary (Live Session, Bridge Environment, …).
Machine-wide behavior contract and machine facts are carried by the global instruction layer; this file only records project facts a non-Claude agent must know.

## Project boundaries

- csctl is an operator tool FOR Claude Code's own sessions/agents/Remote-Control: it reads `~/.claude` on-disk state, walks `/proc`, and shells out to `claude` + `tmux` (tmux-first, ADR-0001). Linux/WSL only.
- Contribution constraints (CONTRIBUTING.md): type hints everywhere; NO hardcoded machine paths — the guardrail `grep -rn --include='*.py' '/home/' src/` must return nothing.
- Version is single-sourced in `src/cc_session_control/__init__.py`; bump only via `python scripts/bump_version.py {patch|minor|major}`; an annotated `vX.Y.Z` tag publishes to PyPI via Trusted Publishing.
- Deliberate exception to the machine-wide no-error-swallowing rule: data functions swallow errors and return safe empties — the TUI must never crash. Do not "fix" this.
- Tests: `uv run --extra dev pytest tests/`; prefer `tmp_path`/`monkeypatch` fakes over touching live `~/.claude` or tmux state.
