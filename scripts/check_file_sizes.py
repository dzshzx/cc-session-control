#!/usr/bin/env python3
"""Reject hand-written source files that exceed the project size limits.

Product source files are capped at 600 lines; test modules get a wider
1000-line hard cap and are checked explicitly via ``--tests`` — the gate
never silently exempts them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MAX_LINES = 600
DEFAULT_TEST_MAX_LINES = 1000
TEST_DIRECTORY_NAMES = frozenset({"test", "tests"})


def _python_files(source: Path, *, skip_test_dirs: bool) -> list[Path]:
    return sorted(
        path
        for path in source.rglob("*.py")
        if not skip_test_dirs
        or TEST_DIRECTORY_NAMES.isdisjoint(path.relative_to(source).parts[:-1])
    )


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as source_file:
        return sum(1 for _ in source_file)


def _oversized(
    root: Path,
    max_lines: int,
    *,
    skip_test_dirs: bool,
) -> list[tuple[Path, int, int]]:
    return [
        (path, count, max_lines)
        for path in _python_files(root, skip_test_dirs=skip_test_dirs)
        if (count := _line_count(path)) > max_lines
    ]


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
        help=f"maximum allowed lines per source file (default: {DEFAULT_MAX_LINES})",
    )
    parser.add_argument(
        "--tests",
        type=Path,
        default=None,
        help="test tree to inspect with the wider test cap (default: skipped)",
    )
    parser.add_argument(
        "--test-max-lines",
        type=int,
        default=DEFAULT_TEST_MAX_LINES,
        help=(
            f"maximum allowed lines per test file (default: {DEFAULT_TEST_MAX_LINES})"
        ),
    )
    args = parser.parse_args(argv)

    oversized = _oversized(args.source, args.max_lines, skip_test_dirs=True)
    if args.tests is not None:
        oversized += _oversized(
            args.tests,
            args.test_max_lines,
            skip_test_dirs=False,
        )
    for path, count, limit in oversized:
        print(
            f"{path}: {count} lines exceeds limit {limit}",
            file=sys.stderr,
        )
    return int(bool(oversized))


if __name__ == "__main__":
    raise SystemExit(main())
