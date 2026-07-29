"""Validate that a release tag is safe to publish."""

from __future__ import annotations

import argparse
import re
import runpy
import subprocess
import sys
from pathlib import Path

VERSION_FILE = Path("src/cc_session_control/__init__.py")


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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
