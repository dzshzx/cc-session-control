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

    # agents subcommand
    sub.add_parser("agents", help="List background agents")

    # env subcommand
    sub.add_parser("env", help="List bridge environments (current + orphan)")

    return parser


def _apply_workspace(args: argparse.Namespace) -> None:
    if args.workspace:
        from .config import cfg
        cfg.workspace = args.workspace


def _cmd_rc(args: argparse.Namespace) -> None:
    from .data import rc

    if not args.rc_command:
        print("Usage: csctl rc <status|add|rm|up|stop|list>")
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


def _cmd_agents(args: argparse.Namespace) -> None:
    from .actions.agent_ops import job_host
    from .data.registry import read_agent_jobs

    jobs = read_agent_jobs(max_age=0.0)
    if not jobs:
        print("No background agents found.")
        return
    for job in jobs:
        _pid, alive = job_host(job)
        state = "live" if alive else (job.state or "settled")
        tempo = job.tempo or "-"
        name = job.name or job.short
        print(f"  {job.short}  [{state}]  tempo={tempo}  {name}  {job.cwd}")


def _cmd_env(args: argparse.Namespace) -> None:
    from .data import environments, rc

    # Scan RC servers so the env_* namespace is covered too (it has no state
    # file — only a running server references it).
    servers = rc.scan_servers()
    # CURRENT is alive-gated (R3/R6): a zombie session's stale bridge must not be
    # counted as bound. FILE-REFERENCED is the bridge-truthy membership set.
    observed = environments.observe_live(rc_servers=servers, max_age=0.0)
    file_referenced = environments.observe(rc_servers=servers, max_age=0.0)
    # Record every file-referenced env so a later run (after RC toggled off / a job
    # removed) reports it as an orphan = ledger − file-referenced (R6 persistence).
    environments.upsert(file_referenced)
    current = environments.current_envs(observed)
    orphans = environments.orphan_envs(file_referenced)

    print(f"Current bridge environments: {len(current)}")
    for e in current:
        print(f"  {e.env_id}  sid={e.bound_sid or '-'}")

    print(f"Orphan environments (delete manually on claude.ai/code): {len(orphans)}")
    for row in environments.manual_delete_list(file_referenced):
        print(f"  {row['env_id']}  sid={row['bound_sid'] or '-'}")

    print(
        "Note: csctl cannot deregister cloud environments; "
        "the orphan list is inherently incomplete "
        "(environments minted while csctl was not running are not tracked)."
    )


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
    elif args.command == "agents":
        _cmd_agents(args)
    elif args.command == "env":
        _cmd_env(args)
    elif args.command is None:
        _cmd_tui(args)
    else:
        parser.print_help()
