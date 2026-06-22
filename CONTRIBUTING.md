# Contributing

Thanks for your interest in cc-session-control!

## Getting Started

```bash
git clone https://github.com/dzshzx/cc-session-control.git
cd cc-session-control
uv venv && uv pip install -e ".[dev]"
csctl --version
```

## Development

- Run TUI: `csctl`
- Run tests: `python -m pytest tests/`
- Check for hardcoded paths: `grep -rn '/home/' src/`

## Pull Requests

1. Fork the repo and create a branch
2. Make your changes
3. Run tests
4. Submit a PR with a clear description

## Code Style

- Keep each source file under 600 lines
- Use type hints
- Follow existing patterns in the codebase
