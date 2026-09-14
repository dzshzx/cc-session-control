from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from version_approval import (  # noqa: E402
    VersionPlan,
    authorization_kind,
    validate_execution,
)


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
        "user.name=Version Test",
        "-c",
        "user.email=version-test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def _tag(repo: Path, tag: str) -> None:
    _git(
        repo,
        "-c",
        "user.name=Version Test",
        "-c",
        "user.email=version-test@example.invalid",
        "tag",
        "-a",
        tag,
        "-m",
        tag,
    )


def _make_bump_repo(tmp_path: Path, *, baseline: str = "1.2.3") -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "cc_session_control"
    scripts = repo / "scripts"
    package.mkdir(parents=True)
    scripts.mkdir()
    (package / "__init__.py").write_text(
        f'__version__ = "{baseline}"\n', encoding="utf-8"
    )
    shutil.copy2(ROOT / "scripts" / "bump_version.py", scripts)
    shutil.copy2(ROOT / "scripts" / "version_approval.py", scripts)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(origin))
    _git(repo, "init", "--quiet")
    _git(repo, "branch", "-M", "master")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "add", ".")
    _commit(repo, "initial")
    _tag(repo, f"v{baseline}")
    _git(repo, "push", "--quiet", "origin", "master", f"refs/tags/v{baseline}")
    return repo


def _run_bump(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GITHUB_REPOSITORY"] = "dzshzx/example"
    return subprocess.run(
        [sys.executable, "scripts/bump_version.py", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_canonical_digest_matches_cross_language_fixture() -> None:
    plan = VersionPlan(
        repository="dzshzx/example",
        namespace="v",
        baseline="1.2.3",
        target="1.3.0",
    )

    assert plan.canonical_json() == (
        '{"repository":"dzshzx/example","schema":1,"versions":'
        '[{"baseline":"1.2.3","namespace":"v","target":"1.3.0"}]}'
    )
    assert plan.summary() == (
        "sha256:1eed417ccd593af576ef4e828eda87fd02791a4053eef7085a860475c9e5e3be"
    )


def test_exact_next_patch_is_automatic() -> None:
    plan = VersionPlan("dzshzx/example", "v", "1.2.3", "1.2.4")

    assert authorization_kind(plan) == "patch"
    validate_execution(plan, None)


def test_minor_requires_current_plan_summary() -> None:
    plan = VersionPlan("dzshzx/example", "v", "1.2.3", "1.3.0")

    with pytest.raises(ValueError, match="requires explicit approval"):
        validate_execution(plan, None)
    with pytest.raises(ValueError, match="does not match"):
        validate_execution(plan, "sha256:" + "0" * 64)
    validate_execution(plan, plan.summary())


def test_unknown_baseline_and_downgrade_fail_closed() -> None:
    with pytest.raises(ValueError, match="baseline is unknown"):
        validate_execution(None, None)
    with pytest.raises(ValueError, match="downgrade is not allowed"):
        validate_execution(
            VersionPlan("dzshzx/example", "v", "2.0.0", "1.9.9"),
            "sha256:" + "0" * 64,
        )


def test_bump_script_changes_exact_next_patch_without_confirmation(
    tmp_path: Path,
) -> None:
    repo = _make_bump_repo(tmp_path)

    result = _run_bump(repo, "patch")

    assert result.returncode == 0, result.stderr
    assert "authorization=patch" in result.stdout
    assert '__version__ = "1.2.4"' in (
        repo / "src" / "cc_session_control" / "__init__.py"
    ).read_text(encoding="utf-8")


def test_bump_script_previews_minor_and_blocks_write_without_confirmation(
    tmp_path: Path,
) -> None:
    repo = _make_bump_repo(tmp_path)

    preview = _run_bump(repo, "minor", "--plan")
    blocked = _run_bump(repo, "--set", "1.3.0")

    assert preview.returncode == 0, preview.stderr
    assert "authorization=confirmation" in preview.stdout
    assert blocked.returncode != 0
    assert "requires explicit approval" in blocked.stderr
    assert '__version__ = "1.2.3"' in (
        repo / "src" / "cc_session_control" / "__init__.py"
    ).read_text(encoding="utf-8")


def test_bump_script_accepts_matching_confirmed_minor_plan(tmp_path: Path) -> None:
    repo = _make_bump_repo(tmp_path)
    preview = _run_bump(repo, "minor", "--plan")
    summary = next(
        line.removeprefix("version_plan_summary=")
        for line in preview.stdout.splitlines()
        if line.startswith("version_plan_summary=")
    )

    result = _run_bump(
        repo,
        "minor",
        "--confirmed-version-plan",
        summary,
    )

    assert result.returncode == 0, result.stderr
    assert '__version__ = "1.3.0"' in (
        repo / "src" / "cc_session_control" / "__init__.py"
    ).read_text(encoding="utf-8")


def test_bump_script_rejects_local_downgrade_even_if_remote_looks_like_patch(
    tmp_path: Path,
) -> None:
    repo = _make_bump_repo(tmp_path, baseline="1.2.3")
    version_file = repo / "src" / "cc_session_control" / "__init__.py"
    version_file.write_text('__version__ = "2.0.0"\n', encoding="utf-8")

    result = _run_bump(repo, "--set", "1.2.4")

    assert result.returncode != 0
    assert "version downgrade is not allowed: 2.0.0 -> 1.2.4" in result.stderr
    assert '__version__ = "2.0.0"' in version_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("arguments", [("major",), ("minor",), ("--set", "2.0.0")])
def test_explicit_grade_or_set_does_not_authorize_write(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    repo = _make_bump_repo(tmp_path)
    version_file = repo / "src" / "cc_session_control" / "__init__.py"
    before = version_file.read_bytes()

    blocked = _run_bump(repo, *arguments)

    assert blocked.returncode != 0
    assert "requires explicit approval" in blocked.stderr
    assert version_file.read_bytes() == before


def test_remote_drift_invalidates_approval_before_file_write(tmp_path: Path) -> None:
    repo = _make_bump_repo(tmp_path)
    summary = VersionPlan("dzshzx/example", "v", "1.2.3", "1.3.0").summary()
    _tag(repo, "v1.2.4")
    _git(repo, "push", "--quiet", "origin", "refs/tags/v1.2.4")
    version_file = repo / "src" / "cc_session_control" / "__init__.py"
    before = version_file.read_bytes()

    blocked = _run_bump(repo, "minor", "--confirmed-version-plan", summary)

    assert blocked.returncode != 0
    assert "does not match" in blocked.stderr
    assert version_file.read_bytes() == before


def test_target_change_invalidates_approval_before_file_write(tmp_path: Path) -> None:
    repo = _make_bump_repo(tmp_path)
    summary = VersionPlan("dzshzx/example", "v", "1.2.3", "1.3.0").summary()

    blocked = _run_bump(repo, "--set", "1.4.0", "--confirmed-version-plan", summary)

    assert blocked.returncode != 0
    assert "does not match" in blocked.stderr
    assert '__version__ = "1.2.3"' in (
        repo / "src" / "cc_session_control" / "__init__.py"
    ).read_text(encoding="utf-8")


def test_local_only_tags_do_not_change_remote_plan(tmp_path: Path) -> None:
    repo = _make_bump_repo(tmp_path)
    _tag(repo, "v9.0.0")

    result = _run_bump(repo, "patch")

    assert result.returncode == 0, result.stderr
    assert '"baseline":"1.2.3"' in result.stdout


def test_unknown_remote_baseline_blocks_write(tmp_path: Path) -> None:
    repo = _make_bump_repo(tmp_path)
    empty_remote = tmp_path / "empty.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(empty_remote))
    _git(repo, "remote", "set-url", "origin", str(empty_remote))
    version_file = repo / "src" / "cc_session_control" / "__init__.py"
    before = version_file.read_bytes()

    blocked = _run_bump(repo, "patch")

    assert blocked.returncode != 0
    assert "baseline is unknown" in blocked.stderr
    assert version_file.read_bytes() == before


def test_no_change_does_not_accept_stale_confirmation() -> None:
    plan = VersionPlan("dzshzx/example", "v", "1.2.3", "1.3.0")
    with pytest.raises(ValueError, match="does not match"):
        validate_execution(plan, "sha256:" + "0" * 64, no_change=True)
