"""Validate that a release tag is safe to publish."""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

VERSION_FILE = Path("src/cc_session_control/__init__.py")
CHANGELOG_FILE = Path("CHANGELOG.md")
_REMOTE_OWNER_REPO = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$")


def _origin_owner_repo() -> tuple[str, str] | None:
    """``owner/repo`` for the ``gh`` query: Actions' own ``GITHUB_REPOSITORY``
    when set, otherwise parsed from the ``origin`` remote URL (SSH or HTTPS)
    for the local pre-tag run."""
    from_actions = os.environ.get("GITHUB_REPOSITORY", "")
    if from_actions.count("/") == 1:
        owner, repo = from_actions.split("/")
        return owner, repo
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        return None
    match = _REMOTE_OWNER_REPO.search(remote.stdout.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    tag = parser.parse_args().tag

    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) is None:
        print(
            "release tag must be exactly vMAJOR.MINOR.PATCH",
            file=sys.stderr,
        )
        return 1

    tag_type = subprocess.run(
        ["git", "cat-file", "-t", f"refs/tags/{tag}"],
        capture_output=True,
        text=True,
    )
    if tag_type.returncode != 0:
        print(
            f"release tag cannot be resolved: {tag_type.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    if tag_type.stdout.strip() != "tag":
        print("release tag must be annotated", file=sys.stderr)
        return 1

    version = runpy.run_path(str(VERSION_FILE))["__version__"]
    if tag != f"v{version}":
        print(
            f"release tag {tag} does not match package version {version}",
            file=sys.stderr,
        )
        return 1

    changelog_text = CHANGELOG_FILE.read_text(encoding="utf-8")
    changelog_heading = re.search(r"^## (\S+)", changelog_text, re.MULTILINE)
    if changelog_heading is None:
        print(f"{CHANGELOG_FILE} has no '## X.Y.Z' heading", file=sys.stderr)
        return 1
    if changelog_heading.group(1) != version:
        print(
            f"{CHANGELOG_FILE} top entry {changelog_heading.group(1)} does not "
            f"match package version {version}",
            file=sys.stderr,
        )
        return 1

    tag_commit = subprocess.run(
        ["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if tag_commit.returncode != 0:
        print(
            f"release tag cannot be peeled to a commit: {tag_commit.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    head_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if head_commit.returncode != 0:
        print(
            f"checkout HEAD cannot be resolved: {head_commit.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    if tag_commit.stdout.strip() != head_commit.stdout.strip():
        print("release tag does not point to checkout HEAD", file=sys.stderr)
        return 1

    master_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", "origin/master"],
        capture_output=True,
        text=True,
    )
    if master_ancestor.returncode != 0:
        print("release tag commit is not on origin/master", file=sys.stderr)
        return 1

    owner_repo = _origin_owner_repo()
    if owner_repo is None:
        print("origin remote URL cannot be parsed into owner/repo", file=sys.stderr)
        return 1
    owner, repo = owner_repo
    tag_sha = tag_commit.stdout.strip()

    try:
        ci_runs = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                f"{owner}/{repo}",
                "--commit",
                tag_sha,
                "--workflow",
                "CI",
                "--json",
                "status,conclusion",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print(
            "gh CLI is required to verify the CI result for the release commit",
            file=sys.stderr,
        )
        return 1
    if ci_runs.returncode != 0:
        print(
            f"gh run list failed: {ci_runs.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    try:
        runs = json.loads(ci_runs.stdout)
    except json.JSONDecodeError:
        print("gh run list returned output that is not JSON", file=sys.stderr)
        return 1
    if not runs:
        print(
            f"no CI workflow run found for commit {tag_sha}",
            file=sys.stderr,
        )
        return 1
    latest_run = runs[0]
    if latest_run["status"] != "completed":
        print(
            f"CI workflow run for commit {tag_sha} is not completed "
            f"(status={latest_run['status']})",
            file=sys.stderr,
        )
        return 1
    if latest_run["conclusion"] != "success":
        print(
            f"CI workflow run for commit {tag_sha} did not succeed "
            f"(conclusion={latest_run['conclusion']})",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
