# Releasing `cc-session-control`

This guide is for maintainers publishing `csctl` so users of the supported
agent CLIs can install it for their own local sessions.

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
machine, signs in to the agent CLIs they use, and runs it against those CLIs'
local state homes (`~/.claude`, `~/.codex`, `~/.kimi-code`), `tmux`, and
workspace state.

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

### TestPyPI dry run (recommended before the first real publish)

A dedicated workflow, `.github/workflows/release-testpypi.yml`, publishes the
full pipeline to TestPyPI on a manual `workflow_dispatch`. Its publish job only
starts after the shared automated quality gate passes. TestPyPI is a separate
index from PyPI, so uploading a version there does **not** consume that version
on the real PyPI — you can dry-run `0.4.0` on TestPyPI and still publish `0.4.0`
to PyPI afterward.

One-time setup, mirroring the PyPI steps above but on TestPyPI:

1. Create a pending publisher (or the project) for `cc-session-control` on
   <https://test.pypi.org>.
2. Create a GitHub environment named `testpypi` in this repository.
3. Add a trusted publisher on the TestPyPI project:

```text
Publisher: GitHub Actions
Owner: dzshzx
Repository: cc-session-control
Workflow name: release-testpypi.yml
Environment name: testpypi
```

Then trigger it from the Actions tab, or:

```bash
gh workflow run release-testpypi.yml --ref master
```

Verify the package page at <https://test.pypi.org/project/cc-session-control/>
before tagging a real release.

Do not store a long-lived PyPI token in GitHub secrets for the normal release
path.

## Pre-Release Checks

Complete the
[Claude Code compatibility checklist](claude-code-compatibility.md) before
tagging. Tier 1 is required for every release. Tier 2 is also required when
the candidate Claude Code version differs from the last semantic verification,
or when any command/schema evidence changes. Record the version, date, exit
statuses, and anything not proved.

Then run the local quality gate:

```bash
uv run --extra dev ruff check src tests scripts
uv run --extra dev ruff format --check src tests scripts
uv run --extra dev mypy src/
uv run --extra dev python scripts/check_file_sizes.py --tests tests
uv run --extra dev pytest tests/ \
  --cov=cc_session_control --cov-branch \
  --cov-report=term-missing --cov-report=json
uv run --extra dev python scripts/check_coverage.py coverage.json
if grep -rn --include='*.py' '/home/' src/; then
  exit 1
fi
uv build --no-sources
uv run --isolated --no-project --with dist/*.whl csctl --version
uv run --isolated --no-project --with dist/*.tar.gz csctl --version
```

If `grep` prints any product-code path under `/home/`, fix it before release.
GitHub runs the same gate from `.github/workflows/quality-gate.yml` before CI
builds and before either PyPI publish workflow can build or upload artifacts.

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

The `Release` workflow runs on `v*` tags. After the shared quality gate passes,
it also verifies that the triggering tag is an exact `vMAJOR.MINOR.PATCH`
annotated tag, matches the package version, and points to the checked-out
commit. Only then does it build the distributions, smoke test the wheel and
source distribution, upload the built artifacts to the workflow run, and
publish to PyPI through Trusted Publishing. Production publishing has no manual
workflow trigger; use the manual TestPyPI workflow for dry runs.

## Post-Release Verification

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
