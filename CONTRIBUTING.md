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

- Run TUI: `csctl`
- Run tests: `uv run --extra dev pytest tests/` (or `python -m pytest tests/` inside the venv)
- Check for hardcoded paths: `grep -rn --include='*.py' '/home/' src/`

## Pull Requests

1. Fork the repo and create a branch
2. Make your changes
3. Run tests
4. Submit a PR with a clear description

## Code Style

- Keep each source file under 600 lines
- Use type hints
- Follow existing patterns in the codebase

## Releasing / version bump

The version lives in **one place**: `__version__` in `src/cc_session_control/__init__.py`.
`pyproject.toml` derives its version from that attribute (setuptools `dynamic`), and
`csctl --version` reads the same attribute, so there is nothing to keep in sync.

Use the helper to bump it (never hand-edit two files):

```bash
python scripts/bump_version.py patch    # 0.2.1 -> 0.2.2
python scripts/bump_version.py minor    # 0.2.1 -> 0.3.0
python scripts/bump_version.py major    # 0.2.1 -> 1.0.0
python scripts/bump_version.py --set 1.2.3   # explicit
python scripts/bump_version.py --show        # print current, no change
```

It edits only `__init__.py` and prints the suggested commit + tag commands.
