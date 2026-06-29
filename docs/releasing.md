# Releasing `cc-session-control`

This guide is for maintainers publishing `csctl` so other Claude Code users can
install it for their own local sessions.

## Release Model

- Package name: `cc-session-control`
- CLI command: `csctl`
- Version source: `src/cc_session_control/__init__.py`
- Build command: `uv build --no-sources`
- Publish path: GitHub Actions + PyPI Trusted Publishing
- Normal user install:

```bash
uv tool install cc-session-control
```

`csctl` is local-machine tooling. Each user installs it on their own Linux/WSL
machine, signs in to their own Claude Code CLI, and runs it against their own
`~/.claude`, `tmux`, and workspace state.

## One-Time PyPI Setup

These steps happen in the PyPI/TestPyPI web UI and cannot be committed to the
repository.

1. If the project does not exist yet, create a pending publisher for
   `cc-session-control` on PyPI. If the project already exists, add the trusted
   publisher under that project.
2. Create a GitHub environment named `pypi` in this repository.
3. In the PyPI project, add a trusted publisher with these values:

```text
Publisher: GitHub Actions
Owner: dzshzx
Repository: cc-session-control
Workflow name: release.yml
Environment name: pypi
```

For a dry run on TestPyPI, create a separate workflow or temporarily change the
publish step to use:

```yaml
with:
  repository-url: https://test.pypi.org/legacy/
```

Do not store a long-lived PyPI token in GitHub secrets for the normal release
path.

## Pre-Release Checks

Run these locally before tagging:

```bash
uv run --extra dev pytest tests/
if grep -rn --include='*.py' '/home/' src/; then
  exit 1
fi
uv build --no-sources
uv run --isolated --no-project --with dist/*.whl csctl --version
uv run --isolated --no-project --with dist/*.tar.gz csctl --version
```

If `grep` prints any product-code path under `/home/`, fix it before release.

## Version Bump

The version lives in one place:

```bash
python scripts/bump_version.py patch
# or
python scripts/bump_version.py --set 0.4.1
```

Commit the version bump and any release notes before tagging.

## Tag And Publish

Use an annotated tag that matches the package version:

```bash
git tag -a v0.4.1 -m "v0.4.1"
git push origin master --tags
```

The `Release` workflow runs on `v*` tags. It builds the distributions, smoke
tests the wheel and source distribution, uploads the built artifacts to the
workflow run, and publishes to PyPI through Trusted Publishing.

## Post-Release Verification

> **First publish only.** After the package is live on PyPI for the first time,
> remove the "Coming soon to PyPI" note from `README.md` (under *Installation*)
> and reword the *Latest `master` build* section so it no longer implies the
> package is unpublished.

On a clean machine or isolated environment:

```bash
uv tool install cc-session-control
csctl --version
csctl --help
```

For an upgrade test:

```bash
uv tool upgrade cc-session-control
csctl --version
```

If a bad version reaches PyPI, do not try to overwrite it. Fix the issue, bump
to the next patch version, and publish a new tag.
