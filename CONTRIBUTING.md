# Contributing

Thanks for your interest in cc-session-control!

## Getting Started

```bash
git clone https://github.com/dzshzx/cc-session-control.git
cd cc-session-control
uv venv && uv pip install -e ".[dev]"
uv run csctl --version
```

> This editable install is **for development only**. To *use* csctl, install it as shown in
> the [README](README.md) — don't rely on a local editable install as your day-to-day
> `csctl`.

## Development

- Run the development TUI: `uv run csctl`
- Run the complete local quality gate — the same script CI runs:

```bash
scripts/check.sh
```

It runs Ruff lint and format checks, mypy, the hardcoded-`/home/` path guard,
the test suite with branch coverage, and the coverage ratchet (independent
statement and branch floors held in `scripts/check_coverage.py`). Remove
`.coverage` and `coverage.json` after local inspection; both are ignored by Git.

## Pull Requests

1. Fork the repo and create a branch
2. Make your changes
3. Run the complete local quality gate above
4. Submit a PR with a clear description

## Code Style

- Treat file size as a design signal, not a line budget. Split a file when it
  mixes several independently-nameable responsibilities, or when the change in
  front of you makes it materially harder to navigate, test, or review — the
  `codex_*.py` sidecars beside `codex.py` are the existing shape. Do not split
  merely to shorten a file, and do not grow one past what a reader can hold in
  mind at once.
- Use type hints
- Follow existing patterns in the codebase

## Releasing / version bump

The version lives in one place (`__version__` in `src/cc_session_control/__init__.py`;
`pyproject.toml` and `csctl --version` both read it) and is bumped only through
`scripts/bump_version.py` (`--help` lists the modes). The release procedure — bump,
green candidate CI, annotated tag, Trusted Publishing — is in
[docs/releasing.md](docs/releasing.md).
