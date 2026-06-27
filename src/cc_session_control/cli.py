"""CLI entry point for csctl."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="csctl",
        description="TUI manager for Claude Code sessions and Remote Control",
    )
    parser.add_argument("--version", action="version", version=f"csctl {__version__}")
    parser.add_argument("--workspace", type=Path, help="Override workspace root directory")

    sub = parser.add_subparsers(dest="command")

    # rc subcommand group
    rc_parser = sub.add_parser("rc", help="Remote Control management")
    rc_sub = rc_parser.add_subparsers(dest="rc_command")
    rc_sub.add_parser("status", help="Show RC status for all projects")
    rc_add = rc_sub.add_parser("add", help="Add project to RC list and start")
    rc_add.add_argument("project", nargs="?", default=".", help="Project name or '.' for current dir")
    rc_rm = rc_sub.add_parser("rm", help="Remove project from RC list and stop")
    rc_rm.add_argument("project", help="Project name")
    rc_sub.add_parser("up", help="Start all listed projects")
    rc_stop = rc_sub.add_parser("stop", help="Stop RC for a project")
    rc_stop.add_argument("target", help="Project name or 'all'")
    rc_sub.add_parser("list", help="Show enabled project list")

    # prune subcommand
    prune_parser = sub.add_parser("prune", help="Clean up sessions")
    prune_parser.add_argument("--max-prompts", type=int, default=0, help="Max prompt count to prune (default: 0)")
    prune_parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry run)")
    prune_parser.add_argument("--sweep-orphans", action="store_true", help="Clean orphan artifact directories")

    return parser


def _apply_workspace(args: argparse.Namespace) -> None:
    if args.workspace:
        from .config import cfg
        cfg.workspace = args.workspace


def _cmd_rc(args: argparse.Namespace) -> None:
    from .data import rc

    if not args.rc_command:
        print("用法: csctl rc <status|add|rm|up|stop|list>")
        sys.exit(1)

    sub = args.rc_command

    if sub == "status":
        projects = rc.scan()
        for p in projects:
            icon = {"running": "[running]", "dead": "[dead   ]", "stopped": "[stopped]"}.get(p.status, p.status)
            auto = "auto" if p.auto_start else "    "
            print(f"  {icon} {auto}  {p.name}")

    elif sub == "add":
        proj = args.project
        if proj == ".":
            ws = str(rc.cfg.workspace)
            cwd = os.getcwd()
            if cwd.startswith(ws + "/"):
                proj = cwd[len(ws) + 1:].split("/")[0]
            else:
                print(f"Current directory is not under {ws}. Specify project name explicitly.")
                sys.exit(1)
        if not rc.is_trusted(proj):
            print(f"Not trusted: {proj} — run 'claude' in that directory first to accept the trust dialog")
            sys.exit(1)
        rc.list_add(proj)
        print(f"Added to list: {proj}")
        ok = rc.start_one(proj)
        if ok:
            print(f"Started: ws/{proj}")

    elif sub == "rm":
        rc.list_rm(args.project)
        rc.stop_one(args.project)
        print(f"Removed and stopped: {args.project}")

    elif sub == "up":
        enabled = rc.list_enabled()
        if not enabled:
            print("List is empty")
            return
        count = rc.start_many(enabled)
        print(f"Started {count} project(s)")

    elif sub == "stop":
        if args.target == "all":
            rc.stop_all()
            print("Stopped all")
        else:
            ok = rc.stop_one(args.target)
            print(f"Stopped {args.target}" if ok else f"Not running: {args.target}")

    elif sub == "list":
        for name in rc.list_enabled():
            print(name)


def _cmd_prune(args: argparse.Namespace) -> None:
    from .data.cleanup import (
        cleanup_stats,
        list_orphan_dirs,
        prune_sessions,
        remove_orphan_dirs,
        remove_session,
    )
    from .data.sessions import scan

    sessions = scan()
    stats = cleanup_stats(sessions)
    print(f"Total: {stats['total']}  Empty: {stats['empty']}  Short(<=2): {stats['short']}  Orphans: {stats['orphans']}")

    if args.sweep_orphans:
        orphans = list_orphan_dirs(sessions)
        print(f"Would sweep {len(orphans)} orphan artifact dir(s)")
        if not args.apply:
            print("Dry run. Add --apply to execute.")
            return
        count = remove_orphan_dirs(sessions)
        print(f"Swept {count} orphan dir(s).")
        return

    targets = prune_sessions(sessions, max_prompts=args.max_prompts)
    print(f"Would prune {len(targets)} session(s) (<={args.max_prompts} prompts)")

    if not args.apply:
        print("Dry run. Add --apply to execute.")
        return

    for s in targets:
        remove_session(s)
    print(f"Pruned {len(targets)} session(s).")


def _cmd_tui(args: argparse.Namespace) -> None:
    from .actions.session_ops import do_resume
    from .app import App

    app = App()
    result = app.run()

    if result and isinstance(result, tuple) and result[0] == "resume":
        _, session, fork = result
        do_resume(session, fork=fork)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _apply_workspace(args)

    if args.command == "rc":
        _cmd_rc(args)
    elif args.command == "prune":
        _cmd_prune(args)
    elif args.command is None:
        _cmd_tui(args)
    else:
        parser.print_help()
