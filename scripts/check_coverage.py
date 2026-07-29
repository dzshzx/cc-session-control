#!/usr/bin/env python3
"""Enforce independent statement and branch coverage floors."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

STATEMENT_FLOOR = 91.0
BRANCH_FLOOR = 82.0


def _coverage_totals(report: Path) -> tuple[float, float]:
    data: Any = json.loads(report.read_text(encoding="utf-8"))
    totals = data["totals"]
    return (
        float(totals["percent_statements_covered"]),
        float(totals["percent_branches_covered"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="coverage.py JSON report")
    parser.add_argument(
        "--statement-floor",
        type=float,
        default=STATEMENT_FLOOR,
        help=f"minimum statement coverage (default: {STATEMENT_FLOOR:g})",
    )
    parser.add_argument(
        "--branch-floor",
        type=float,
        default=BRANCH_FLOOR,
        help=f"minimum branch coverage (default: {BRANCH_FLOOR:g})",
    )
    args = parser.parse_args(argv)

    statements, branches = _coverage_totals(args.report)
    failures: list[str] = []
    if statements < args.statement_floor:
        failures.append(
            f"statement coverage {statements:.2f}% is below required "
            f"{args.statement_floor:.2f}%"
        )
    if branches < args.branch_floor:
        failures.append(
            f"branch coverage {branches:.2f}% is below required "
            f"{args.branch_floor:.2f}%"
        )
    for failure in failures:
        print(failure, file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
