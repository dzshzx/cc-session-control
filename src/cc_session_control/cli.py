"""Parser and dispatch entry point for ``csctl``."""

from __future__ import annotations

import argparse
from typing import Protocol, TextIO

from . import cli_commands
from .cli_streams import run_with_streams


class CommandHandler(Protocol):
    """Uniform interface bound to every leaf command parser.

    Each ``_cmd_*`` function is bound once via ``set_defaults(handler=...)``;
    `dispatch` is the single place that applies the stream-injection boundary
    (`cli_streams.run_with_streams`) around whichever handler argparse
    resolved.
    """

    def __call__(self, args: argparse.Namespace) -> int: ...


def build_parser() -> argparse.ArgumentParser:
    """Build the complete command tree without loading runtime configuration."""
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="csctl",
        description="tmux-first workbench for Claude Code, Codex CLI, and Kimi Code",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"csctl {__version__}",
    )
    parser.add_argument(
        "--theme",
        choices=("auto", "dark", "light"),
        help=("TUI palette (default: $COLORFGBG if set, else dark; env CSCTL_THEME)"),
    )

    commands = parser.add_subparsers(dest="command")

    resume = commands.add_parser(
        "resume",
        help=("List resumable sessions across directories and print resume commands"),
    )
    resume.add_argument(
        "keyword",
        nargs="?",
        default="",
        help="Filter: sid/cwd/title, then transcript body",
    )
    resume.add_argument(
        "--page",
        type=int,
        default=1,
        help="Page number (default: 1)",
    )
    resume.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Sessions per page (default: 20)",
    )
    resume.add_argument(
        "--all",
        action="store_true",
        dest="all_pages",
        help="List everything, no paging",
    )
    resume.add_argument(
        "--take-over",
        metavar="SID",
        help=(
            "Re-resolve one exact session id and resume it through the "
            "execution-time takeover safety gates (Claude sessions only; "
            "resume a codex/kimi session directly with its own CLI instead)"
        ),
    )
    resume.set_defaults(handler=cli_commands._cmd_resume)

    agents = commands.add_parser("agents", help="List background agents")
    agents.set_defaults(handler=cli_commands._cmd_agents)

    return parser


def apply_global_flags(args: argparse.Namespace) -> None:
    """Apply validated process-wide options after help/version parsing."""
    from .config import cfg

    if args.theme:
        cfg.theme = args.theme


def dispatch(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Dispatch one parsed command exactly once, or start the TUI.

    This is the single boundary where caller-owned streams are injected
    (`cli_streams.run_with_streams`) — every `_cmd_*` handler, bound once via
    `set_defaults(handler=...)` when its parser was built, runs through it.
    """
    if args.command is None:
        return run_with_streams(
            cli_commands._cmd_tui,
            args,
            stdout=stdout,
            stderr=stderr,
        )
    handler: CommandHandler | None = getattr(args, "handler", None)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
    return run_with_streams(handler, args, stdout=stdout, stderr=stderr)


def main(argv: list[str] | None = None) -> int:
    """Run ``csctl`` for ``argv`` and return its process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        apply_global_flags(args)
    except ValueError as exc:
        parser.error(str(exc))
    return dispatch(args, parser)
