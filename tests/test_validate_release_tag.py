import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
VALIDATOR = ROOT / "scripts" / "validate_release_tag.py"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str) -> None:
    _git(
        repo,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release-test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def _annotated_tag(repo: Path, tag: str) -> None:
    _git(
        repo,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release-test@example.invalid",
        "tag",
        "-a",
        tag,
        "-m",
        tag,
    )


def _make_repo(
    tmp_path: Path,
    version: str = "1.2.3",
    changelog_version: str | None = None,
    changelog_heading: bool = True,
) -> Path:
    """Build a git repo shaped like this project: __init__.py version, a
    matching CHANGELOG.md top entry (unless overridden), and an origin
    remote the validator's CI-status check can parse an owner/repo out of.
    """
    repo = tmp_path / "repo"
    package = repo / "src" / "cc_session_control"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    if changelog_heading:
        heading_version = (
            changelog_version if changelog_version is not None else version
        )
        (repo / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {heading_version} (2026-01-01)\n\n- notes\n",
            encoding="utf-8",
        )
    else:
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\nno headings here\n", encoding="utf-8"
        )
    _git(repo, "init", "--quiet")
    _git(repo, "branch", "-M", "master")
    _git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
    _git(repo, "add", ".")
    _commit(repo, "initial")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    return repo


def _fake_bin_dir(
    tmp_path: Path,
    *,
    include_gh: bool,
    gh_json: str = "[]",
    gh_returncode: int = 0,
    name: str = "fakebin",
) -> str:
    """A PATH entry containing a real `git` (symlinked) and, optionally, a
    stub `gh` that prints fixed JSON — this is how the gh-dependent CI-status
    check is exercised without hitting the network or a real gh CLI.
    """
    bin_dir = tmp_path / name
    bin_dir.mkdir(exist_ok=True)

    git_path = shutil.which("git")
    assert git_path is not None, "git must be on PATH to build the test fixture"
    (bin_dir / "git").symlink_to(git_path)

    if include_gh:
        gh_script = bin_dir / "gh"
        # `#!/bin/sh` is resolved by the kernel directly from the shebang, and
        # `printf` is a shell builtin — both work even though PATH below is
        # restricted to this directory, unlike `#!/usr/bin/env bash` + `cat`.
        gh_script.write_text(
            f"#!/bin/sh\nprintf '%s' '{gh_json}'\nexit {gh_returncode}\n",
            encoding="utf-8",
        )
        mode = gh_script.stat().st_mode
        gh_script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return str(bin_dir)


def _run_validator(
    repo: Path, tag: str, *, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(VALIDATOR), tag],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_matching_annotated_tag_passes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.3")
    path = _fake_bin_dir(
        tmp_path,
        include_gh=True,
        gh_json='[{"status": "completed", "conclusion": "success"}]',
    )

    result = _run_validator(repo, "v1.2.3", path=path)

    assert result.returncode == 0, result.stderr


def test_lightweight_tag_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _git(repo, "tag", "v1.2.3")

    result = _run_validator(repo, "v1.2.3")

    assert result.returncode != 0
    assert "annotated" in result.stderr


def test_annotated_tag_with_wrong_version_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.4")

    result = _run_validator(repo, "v1.2.4")

    assert result.returncode != 0
    assert "package version 1.2.3" in result.stderr


def test_malformed_v_tag_fails_before_git_lookup(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    result = _run_validator(repo, "v1.2")

    assert result.returncode != 0
    assert "exactly vMAJOR.MINOR.PATCH" in result.stderr
    assert "cannot be resolved" not in result.stderr


def test_annotated_tag_on_different_commit_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.3")
    (repo / "README.md").write_text("later commit\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _commit(repo, "later")

    result = _run_validator(repo, "v1.2.3")

    assert result.returncode != 0
    assert "does not point to checkout HEAD" in result.stderr


def test_annotated_tag_outside_origin_master_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("release branch\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _commit(repo, "release branch")
    _annotated_tag(repo, "v1.2.3")

    result = _run_validator(repo, "v1.2.3")

    assert result.returncode != 0
    assert "not on origin/master" in result.stderr


def test_changelog_top_entry_mismatch_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, changelog_version="1.2.2")
    _annotated_tag(repo, "v1.2.3")

    result = _run_validator(repo, "v1.2.3")

    assert result.returncode != 0
    assert "CHANGELOG.md top entry 1.2.2 does not" in result.stderr
    assert "match package version 1.2.3" in result.stderr


def test_changelog_missing_heading_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, changelog_heading=False)
    _annotated_tag(repo, "v1.2.3")

    result = _run_validator(repo, "v1.2.3")

    assert result.returncode != 0
    assert "has no '## X.Y.Z' heading" in result.stderr


def test_ci_status_pending_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.3")
    path = _fake_bin_dir(
        tmp_path,
        include_gh=True,
        gh_json='[{"status": "in_progress", "conclusion": null}]',
    )

    result = _run_validator(repo, "v1.2.3", path=path)

    assert result.returncode != 0
    assert "is not completed (status=in_progress)" in result.stderr


def test_ci_status_failure_conclusion_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.3")
    path = _fake_bin_dir(
        tmp_path,
        include_gh=True,
        gh_json='[{"status": "completed", "conclusion": "failure"}]',
    )

    result = _run_validator(repo, "v1.2.3", path=path)

    assert result.returncode != 0
    assert "did not succeed (conclusion=failure)" in result.stderr


def test_ci_status_no_run_found_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.3")
    path = _fake_bin_dir(tmp_path, include_gh=True, gh_json="[]")

    result = _run_validator(repo, "v1.2.3", path=path)

    assert result.returncode != 0
    assert "no CI workflow run found for commit" in result.stderr


def test_ci_status_missing_gh_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.3")
    path = _fake_bin_dir(tmp_path, include_gh=False)

    result = _run_validator(repo, "v1.2.3", path=path)

    assert result.returncode != 0
    assert "gh CLI is required" in result.stderr
