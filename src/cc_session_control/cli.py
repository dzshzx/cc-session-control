"""CLI entry point for csctl."""

from __future__ import annotations

import argparse
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="csctl",
        description="TUI manager for Claude Code sessions and Remote Control",
    )
    parser.add_argument("--version", action="version", version=f"csctl {__version__}")
    parser.add_argument(
        "--theme", choices=("auto", "dark", "light"),
        help="TUI palette (default: auto-detect the terminal background; env CSCTL_THEME)",
    )

    sub = parser.add_subparsers(dest="command")

    # rc subcommand group
    rc_parser = sub.add_parser("rc", help="Remote Control management")
    rc_sub = rc_parser.add_subparsers(dest="rc_command")
    rc_sub.add_parser("status", help="Show RC status for all projects")
    rc_add = rc_sub.add_parser("add", help="Add project to RC list and start")
    rc_add.add_argument("project", nargs="?", default=".", help="Project directory (default: current dir)")
    rc_rm = rc_sub.add_parser("rm", help="Remove project from RC list and stop")
    rc_rm.add_argument("project", help="Project directory")
    rc_sub.add_parser("up", help="Start all listed projects")
    rc_stop = rc_sub.add_parser("stop", help="Stop RC for a project")
    rc_stop.add_argument("target", help="Project directory or 'all'")
    rc_sub.add_parser("list", help="Show enabled project list")

    # prune subcommand
    prune_parser = sub.add_parser("prune", help="Clean up sessions")
    prune_parser.add_argument("--max-prompts", type=int, default=0, help="Max prompt count to prune (default: 0)")
    prune_parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry run)")
    prune_parser.add_argument("--sweep-orphans", action="store_true", help="Clean orphan sid-keyed artifact directories")
    prune_parser.add_argument("--sweep-zombies", action="store_true", help="Remove zombie sessions/<pid>.json files (dead procs; keeps current + alive pids)")
    prune_parser.add_argument("--sweep-aged", action="store_true", help="Remove age-keyed global entries older than cleanup_age_days")

    # resume subcommand (headless: list sessions + print ready-to-copy commands)
    resume_parser = sub.add_parser(
        "resume",
        help="List resumable sessions across directories and print resume commands",
    )
    resume_parser.add_argument("keyword", nargs="?", default="", help="Filter: sid/cwd/title, then transcript body")
    resume_parser.add_argument("--page", type=int, default=1, help="Page number (default: 1)")
    resume_parser.add_argument("--limit", type=int, default=20, help="Sessions per page (default: 20)")
    resume_parser.add_argument("--all", action="store_true", dest="all_pages", help="List everything, no paging")

    # skill subcommand group (bundled Claude Code agent skill)
    skill_parser = sub.add_parser("skill", help="Manage the bundled Claude Code skill")
    skill_sub = skill_parser.add_subparsers(dest="skill_command")
    skill_install = skill_sub.add_parser("install", help="Install SKILL.md into ~/.claude/skills/")
    skill_install.add_argument("--force", action="store_true", help="Replace an existing skill directory")
    skill_sub.add_parser("uninstall", help="Remove the installed skill directory")

    # agents subcommand
    sub.add_parser("agents", help="List background agents")

    # env subcommand
    sub.add_parser("env", help="List bridge environments (current + orphan)")

    return parser


def _apply_global_flags(args: argparse.Namespace) -> None:
    from .config import cfg
    if args.theme:
        cfg.theme = args.theme


def _cmd_rc(args: argparse.Namespace) -> None:
    from .data import rc
    from .models import TrustDecision

    if not args.rc_command:
        print("Usage: csctl rc <status|add|rm|up|stop|list>")
        sys.exit(1)

    sub = args.rc_command

    if sub == "status":
        from .data.sessions import scan as scan_sessions

        # Same ordering as the 项目 tab (rc.order_by_activity — single
        # source); costs one transcript scan, like `csctl resume`.
        scan_result = rc.scan_result()
        projects = rc.order_by_activity(scan_result.projects, scan_sessions())
        if not scan_result.settings.available:
            print(
                "Project settings unavailable: "
                f"{scan_result.settings.state.value}"
                f"{': ' + scan_result.settings.detail if scan_result.settings.detail else ''}"
            )
        for p in projects:
            icon = {"running": "[running]", "dead": "[dead   ]", "stopped": "[stopped]"}.get(p.status, p.status)
            auto = "auto" if p.auto_start else "    "
            missing = "" if p.dir_exists else "  (directory missing)"
            print(f"  {icon} {auto}  {p.name}  {p.directory}{missing}")

    elif sub == "add":
        path = os.path.abspath(args.project)
        if not os.path.isdir(path):
            print(f"No such directory: {path}")
            sys.exit(1)
        trust = rc.project_trust(path)
        if trust.decision is TrustDecision.UNAVAILABLE:
            print(
                "Project settings unavailable: "
                f"{trust.settings.state.value}"
                f"{': ' + trust.settings.detail if trust.settings.detail else ''}"
                " — refusing to start"
            )
            sys.exit(1)
        if trust.decision is TrustDecision.UNTRUSTED:
            print(f"Not trusted: {path} — run 'claude' in that directory first to accept the trust dialog")
            sys.exit(1)
        rc.list_add(path)
        print(f"Added to list: {path}")
        result = rc.start_one_result(path)
        if result.state is rc.StartState.STARTED:
            print(f"Started RC server for {path}")
        elif result.state is rc.StartState.TRUST_UNAVAILABLE:
            print("Project settings became unavailable — refusing to start")
            sys.exit(1)
        elif result.state is rc.StartState.UNTRUSTED:
            print("Project is no longer trusted — refusing to start")
            sys.exit(1)
        else:
            print(f"RC server was not started: {result.state.value}")

    elif sub == "rm":
        path = os.path.abspath(args.project)
        rc.list_rm(path)
        rc.stop_one(path)
        print(f"Removed and stopped: {path}")

    elif sub == "up":
        enabled = rc.list_enabled()
        if not enabled:
            print("List is empty")
            return
        result = rc.start_many_result(enabled)
        print(f"Started {result.started} project(s)")
        if result.unavailable:
            print(
                "Project settings unavailable; refused "
                f"{result.unavailable} project(s)"
            )

    elif sub == "stop":
        if args.target == "all":
            rc.stop_all()
            print("Stopped all")
        else:
            path = os.path.abspath(args.target)
            ok = rc.stop_one(path)
            print(f"Stopped {path}" if ok else f"Not running: {path}")

    elif sub == "list":
        for name in rc.list_enabled():
            print(name)


def _cmd_prune(args: argparse.Namespace) -> None:
    from .data import liveness
    from .data.cleanup import (
        build_plan,
        execute_orphan_removals,
        execute_session_removals,
        prune_sessions,
    )
    from .data.sessions import scan

    sessions = scan()
    # One shared fetch feeds the frozen plan — the SAME assembly build_plan's
    # other callers (the Sessions view, build_world_snapshot) use, so the CLI
    # header and any future TUI parity stay derived from one source (删除 ⊆ 预览
    # still holds: execute_* revalidate against fresh data at apply time).
    procs, cur, jobs, agents = liveness.liveness_inputs()
    plan = build_plan(sessions, procs, cur, jobs, agents)
    counts = plan.counts()
    print(
        f"Total: {len(sessions)}  Prunable empty: {counts['empty']}  "
        f"short(<=2): {counts['short']}  Orphan dirs: {counts['orphan_dirs']}  "
        f"Zombie files: {counts['zombie_procs']}  Aged: {counts['aged_entries']}"
    )

    if args.sweep_orphans:
        orphans = plan.orphan_entries
        print(f"Would sweep {len(orphans)} orphan artifact dir(s)")
        if not args.apply:
            print("Dry run. Add --apply to execute.")
            return
        # Deletes AT MOST the listed entries, revalidated against fresh
        # protection data (删除 ⊆ 预览 — same executor as the TUI; `sessions`
        # feeds the transcript tier of the protection set).
        count = execute_orphan_removals(orphans, sessions=sessions)
        print(f"Swept {count} orphan dir(s).")
        return

    if args.sweep_zombies:
        _cmd_prune_zombies(args)
        return

    if args.sweep_aged:
        _cmd_prune_aged(args)
        return

    targets = prune_sessions(sessions, max_prompts=args.max_prompts)
    print(f"Would prune {len(targets)} session(s) (<={args.max_prompts} prompts)")

    if not args.apply:
        print("Dry run. Add --apply to execute.")
        return

    count = execute_session_removals(targets)
    print(f"Pruned {count} session(s).")


def _cmd_prune_zombies(args: argparse.Namespace) -> None:
    """Strategy A pid-keyed sweep of `sessions/<pid>.json` (R7.1) via the CLI.

    Reuses the already-gated `data/cleanup` helpers: `select_zombie_pids` keeps
    the current session's pid and any alive pid of a resumed multi-pid sid, and
    `execute_zombie_removals` refuses without `/proc`. The dry-run preview is
    gated here too — off `/proc` every pid looks dead, so `current` can't be
    determined and we must not even claim the files are sweepable (R10).
    """
    from .data import liveness, proc
    from .data.cleanup import execute_zombie_removals, select_zombie_pids

    if not proc.current_determinable():
        print("Refused: '/proc' unavailable — cannot determine the current session (R10).")
        return
    procs = liveness.live_session_procs(max_age=0.0)
    cur = proc.ancestor_pids()
    zombies = select_zombie_pids(procs, cur)
    print(f"Would sweep {len(zombies)} zombie session file(s)")
    if not args.apply:
        print("Dry run. Add --apply to execute.")
        return
    count = execute_zombie_removals(zombies, session_procs=procs, cur=cur)
    print(f"Swept {count} zombie session file(s).")


def _cmd_prune_aged(args: argparse.Namespace) -> None:
    """Strategy B age sweep of time/global-keyed dirs (R7.2) via the CLI.

    The age sweep is mtime-only and session-agnostic, so (unlike the zombie
    sweep) it is not gated on `/proc`.
    """
    from .config import cfg
    from .data.cleanup import execute_aged_removals, list_aged_entries

    aged = list_aged_entries()
    print(f"Would sweep {len(aged)} aged entr(y/ies) older than {cfg.cleanup_age_days}d")
    if not args.apply:
        print("Dry run. Add --apply to execute.")
        return
    count = execute_aged_removals(aged)
    print(f"Swept {count} aged entr(y/ies).")


def _cmd_resume(args: argparse.Namespace) -> None:
    from .actions.resume_list import render
    from .data.sessions import scan

    print(render(scan(), keyword=args.keyword, page=args.page,
                 limit=args.limit, all_pages=args.all_pages))


def _cmd_skill(args: argparse.Namespace) -> None:
    from .actions import skill_ops

    if args.skill_command == "install":
        ok, msg = skill_ops.install(force=args.force)
    elif args.skill_command == "uninstall":
        ok, msg = skill_ops.uninstall()
    else:
        print("Usage: csctl skill <install|uninstall>")
        sys.exit(1)
    print(msg)
    if not ok:
        sys.exit(1)


def _cmd_agents(args: argparse.Namespace) -> None:
    from .data.liveness import enrich_jobs
    from .data.registry import read_agent_jobs

    jobs = enrich_jobs(read_agent_jobs(max_age=0.0))
    if not jobs:
        print("No background agents found.")
        return
    for job in jobs:
        state = "live" if job.host_alive else (job.state or "settled")
        tempo = job.tempo or "-"
        name = job.name or job.short
        print(f"  {job.short}  [{state}]  tempo={tempo}  {name}  {job.cwd}")


def _cmd_env(args: argparse.Namespace) -> None:
    from .data import environments, rc

    # Scan RC servers so the env_* namespace is covered too (it has no state
    # file — only a running server references it). The whole observe → upsert →
    # classify pipeline (and its ordering invariant) lives in reconcile.
    recon = environments.reconcile(rc_servers=rc.scan_servers(), max_age=0.0)

    print(f"Current bridge environments: {len(recon.current)}")
    for e in recon.current:
        print(f"  {e.env_id}  sid={e.bound_sid or '-'}")

    print(f"Orphan environments (delete manually on claude.ai/code): {len(recon.orphans)}")
    for e in recon.orphans:
        print(f"  {e.env_id}  sid={e.bound_sid or '-'}")

    print(
        "Note: csctl cannot deregister cloud environments; "
        "the orphan list is inherently incomplete "
        "(environments minted while csctl was not running are not tracked)."
    )


def _cmd_tui(args: argparse.Namespace) -> None:
    from .actions.session_ops import ExitIntent
    from .app import App

    result = App().run()

    # The intent finalizes itself outside the urwid loop (it may exec-replace
    # csctl — resume/attach) and prints its own failure messages.
    if isinstance(result, ExitIntent):
        result.run()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        _apply_global_flags(args)
    except ValueError as exc:
        parser.error(str(exc))

    if args.command == "rc":
        _cmd_rc(args)
    elif args.command == "prune":
        _cmd_prune(args)
    elif args.command == "resume":
        _cmd_resume(args)
    elif args.command == "skill":
        _cmd_skill(args)
    elif args.command == "agents":
        _cmd_agents(args)
    elif args.command == "env":
        _cmd_env(args)
    elif args.command is None:
        _cmd_tui(args)
    else:
        parser.print_help()
