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


def _make_repo(tmp_path: Path, version: str = "1.2.3") -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "cc_session_control"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    _git(repo, "init", "--quiet")
    _git(repo, "branch", "-M", "master")
    _git(repo, "add", ".")
    _commit(repo, "initial")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    return repo


def _run_validator(repo: Path, tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), tag],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_matching_annotated_tag_passes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _annotated_tag(repo, "v1.2.3")

    result = _run_validator(repo, "v1.2.3")

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
