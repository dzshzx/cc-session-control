"""Parser and dispatch entry point for ``csctl``."""

from __future__ import annotations

import argparse
from typing import Protocol, TextIO

from . import cli_commands, cli_rc


class CommandHandler(Protocol):
    """Uniform interface bound to every leaf command parser."""

    def __call__(
        self,
        args: argparse.Namespace,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> int: ...


def build_parser() -> argparse.ArgumentParser:
    """Build the complete command tree without loading runtime configuration."""
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="csctl",
        description="TUI manager for Claude Code sessions and Remote Control",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"csctl {__version__}",
    )
    parser.add_argument(
        "--theme",
        choices=("auto", "dark", "light"),
        help=(
            "TUI palette (default: auto-detect the terminal background; "
            "env CSCTL_THEME)"
        ),
    )

    commands = parser.add_subparsers(dest="command")

    rc_parser = commands.add_parser("rc", help="Remote Control management")
    rc_commands = rc_parser.add_subparsers(dest="rc_command", required=True)
    rc_status = rc_commands.add_parser(
        "status",
        help="Show RC status for all projects",
    )
    rc_status.set_defaults(handler=cli_rc.handle_status)
    rc_add = rc_commands.add_parser(
        "add",
        help="Add project to RC list and start",
    )
    rc_add.add_argument(
        "project",
        nargs="?",
        default=".",
        help="Project directory (default: current dir)",
    )
    rc_add.set_defaults(handler=cli_rc.handle_add)
    rc_rm = rc_commands.add_parser(
        "rm",
        help="Remove project from RC list and stop",
    )
    rc_rm.add_argument("project", help="Project directory")
    rc_rm.set_defaults(handler=cli_rc.handle_rm)
    rc_up = rc_commands.add_parser("up", help="Start all listed projects")
    rc_up.set_defaults(handler=cli_rc.handle_up)
    rc_stop = rc_commands.add_parser(
        "stop",
        help="Stop RC for a project",
    )
    rc_stop.add_argument("target", help="Project directory or 'all'")
    rc_stop.set_defaults(handler=cli_rc.handle_stop)
    rc_list = rc_commands.add_parser(
        "list",
        help="Show enabled project list",
    )
    rc_list.set_defaults(handler=cli_rc.handle_list)

    prune = commands.add_parser("prune", help="Clean up sessions")
    prune.add_argument(
        "--max-prompts",
        type=int,
        default=0,
        help="Max prompt count to prune (default: 0)",
    )
    prune.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete (default: dry run)",
    )
    prune.add_argument(
        "--sweep-orphans",
        action="store_true",
        help="Clean orphan sid-keyed artifact directories",
    )
    prune.add_argument(
        "--sweep-zombies",
        action="store_true",
        help=(
            "Remove zombie sessions/<pid>.json files "
            "(dead procs; keeps current + alive pids)"
        ),
    )
    prune.add_argument(
        "--sweep-aged",
        action="store_true",
        help=("Remove age-keyed global entries older than cleanup_age_days"),
    )
    prune.set_defaults(handler=cli_commands.handle_prune)

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
    resume.set_defaults(handler=cli_commands.handle_resume)

    skill = commands.add_parser(
        "skill",
        help="Manage the bundled Claude Code skill",
    )
    skill_commands = skill.add_subparsers(
        dest="skill_command",
        required=True,
    )
    skill_install = skill_commands.add_parser(
        "install",
        help="Install SKILL.md into ~/.claude/skills/",
    )
    skill_install.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing skill directory",
    )
    skill_install.set_defaults(handler=cli_commands.handle_skill_install)
    skill_uninstall = skill_commands.add_parser(
        "uninstall",
        help="Remove the installed skill directory",
    )
    skill_uninstall.set_defaults(
        handler=cli_commands.handle_skill_uninstall,
    )

    agents = commands.add_parser("agents", help="List background agents")
    agents.set_defaults(handler=cli_commands.handle_agents)

    env = commands.add_parser(
        "env",
        help="List bridge environments (current + orphan)",
    )
    env.set_defaults(handler=cli_commands.handle_env)

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
    """Dispatch one parsed command exactly once, or start the TUI."""
    if args.command is None:
        return cli_commands.handle_tui(
            args,
            stdout=stdout,
            stderr=stderr,
        )
    handler: CommandHandler | None = getattr(args, "handler", None)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
    return handler(args, stdout=stdout, stderr=stderr)


def main(argv: list[str] | None = None) -> int:
    """Run ``csctl`` for ``argv`` and return its process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        apply_global_flags(args)
    except ValueError as exc:
        parser.error(str(exc))
    return dispatch(args, parser)
