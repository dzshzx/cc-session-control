#!/usr/bin/env python3
"""Bump cc-session-control's version.

Single source of truth: ``__version__`` in ``src/cc_session_control/__init__.py``.
``pyproject.toml`` derives its version from that attribute (setuptools dynamic),
so this script only ever edits one file.

Usage:
    python scripts/bump_version.py patch       # 0.2.1 -> 0.2.2
    python scripts/bump_version.py minor       # 0.2.1 -> 0.3.0
    python scripts/bump_version.py major       # 0.2.1 -> 1.0.0
    python scripts/bump_version.py --set 1.2.3 # explicit version
    python scripts/bump_version.py --show      # print current version, no change
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent / "src" / "cc_session_control" / "__init__.py"
_PATTERN = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def read_version(text: str) -> str:
    match = _PATTERN.search(text)
    if not match:
        raise SystemExit(f"__version__ assignment not found in {INIT}")
    return match.group(1)


def bump(version: str, part: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"version {version!r} is not MAJOR.MINOR.PATCH")
    major, minor, patch = (int(p) for p in parts)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("part", nargs="?", choices=["major", "minor", "patch"],
                        help="which component to increment")
    parser.add_argument("--set", dest="explicit", metavar="X.Y.Z",
                        help="set an explicit version instead of bumping")
    parser.add_argument("--show", action="store_true",
                        help="print the current version and exit")
    args = parser.parse_args(argv)

    text = INIT.read_text(encoding="utf-8")
    current = read_version(text)

    if args.show:
        print(current)
        return 0

    if args.explicit:
        new = args.explicit
    elif args.part:
        new = bump(current, args.part)
    else:
        parser.error("specify a part (major/minor/patch) or --set X.Y.Z")

    INIT.write_text(_PATTERN.sub(f'__version__ = "{new}"', text, count=1), encoding="utf-8")
    print(f"{current} -> {new}")
    print(f"next: git commit -am 'chore: bump version to {new}' && git tag v{new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
