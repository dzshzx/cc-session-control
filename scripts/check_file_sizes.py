#!/usr/bin/env python3
"""Reject hand-written source files that exceed the project size limit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MAX_LINES = 600
TEST_DIRECTORY_NAMES = frozenset({"test", "tests"})


def _python_files(source: Path) -> list[Path]:
    return sorted(
        path
        for path in source.rglob("*.py")
        if TEST_DIRECTORY_NAMES.isdisjoint(path.relative_to(source).parts[:-1])
    )


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as source_file:
        return sum(1 for _ in source_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("src"),
        help="source tree to inspect (default: src)",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=f"maximum allowed lines per file (default: {DEFAULT_MAX_LINES})",
    )
    args = parser.parse_args(argv)

    oversized = [
        (path, count)
        for path in _python_files(args.source)
        if (count := _line_count(path)) > args.max_lines
    ]
    for path, count in oversized:
        print(
            f"{path}: {count} lines exceeds limit {args.max_lines}",
            file=sys.stderr,
        )
    return int(bool(oversized))


if __name__ == "__main__":
    raise SystemExit(main())
