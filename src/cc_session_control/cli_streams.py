"""Injected output streams shared by CLI command modules."""

from __future__ import annotations

import sys
from argparse import Namespace
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import TextIO

Command = Callable[[Namespace], int]


def run_with_streams(
    command: Command,
    args: Namespace,
    *,
    stdout: TextIO | None,
    stderr: TextIO | None,
) -> int:
    """Run a command with caller-owned streams at the public handler seam."""
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    with redirect_stdout(output), redirect_stderr(errors):
        return command(args)
