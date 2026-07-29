"""Command handlers and renderers for the ``csctl`` CLI."""

from __future__ import annotations

import sys
from argparse import Namespace
from collections.abc import Mapping, Sequence
from typing import TextIO

from .cli_streams import run_with_streams
from .data.removal import CleanupExecution, CleanupIssue, RemovalAnchor


def _print_cleanup_issues(issues: Sequence[CleanupIssue]) -> int:
    """Surface partial preview evidence and return its process status."""
    for issue in issues:
        where = f" ({issue.path})" if issue.path else ""
        print(
            f"Warning: cleanup preview is partial: {issue.source}{where}: "
            f"{issue.error}",
            file=sys.stderr,
        )
    return int(bool(issues))


def _print_cleanup_execution(
    result: CleanupExecution,
    *,
    success: str,
    subject: str,
) -> int:
    """Print one honest apply outcome and return its process status."""
    completed = len(result.completed)
    incomplete = result.incomplete
    stream = sys.stderr if incomplete else sys.stdout
    details: list[str] = []
    if result.failed and result.removed:
        details.append(f"removed paths {len(result.removed)}")
    if result.failed:
        first = result.failed[0]
        details.append(
            f"failed {len(result.failed)}"
            + (f" ({first.path}: {first.error})" if first.error else "")
        )
    if result.skipped:
        details.append(f"skipped {len(result.skipped)} ({result.skipped[0].reason})")
    if result.refused:
        details.append(f"refused {len(result.refused)} ({result.refused[0].reason})")
    if result.missing_targets:
        details.append(f"already missing {len(result.missing_targets)}")
    if result.issues:
        issue = result.issues[0]
        where = f" ({issue.path})" if issue.path else ""
        details.append(
            "Protection evidence incomplete; nothing deleted: "
            f"{issue.source}{where}: {issue.error}"
        )

    if completed and not details:
        print(success.format(n=completed), file=stream)
    elif completed:
        print(
            f"Partial sweep: removed {completed} {subject}; {'; '.join(details)}.",
            file=stream,
        )
    elif result.removed:
        print(
            f"Partial sweep: completed 0 {subject}; {'; '.join(details)}.",
            file=stream,
        )
    elif result.refused:
        print(
            f"Refused: no {subject} removed; {'; '.join(details)}.",
            file=stream,
        )
    elif result.failed:
        print(
            f"Sweep failed: removed 0 {subject}; {'; '.join(details)}.",
            file=stream,
        )
    elif details:
        print(
            f"No {subject} removed; {'; '.join(details)}.",
            file=stream,
        )
    else:
        print(f"No {subject} removed.", file=stream)
    return int(incomplete)


def _cmd_prune(args: Namespace) -> int:
    # Age-only cleanup is session-agnostic. Keep orphan/zombie precedence for
    # multi-flag invocations by routing early only when neither is requested.
    if args.sweep_aged and not (args.sweep_orphans or args.sweep_zombies):
        from .data.age_cleanup import build_age_plan

        age_plan = build_age_plan()
        plan_status = _print_cleanup_issues(age_plan.issues)
        return max(
            _cmd_prune_aged(
                args,
                list(age_plan.entries),
                age_plan.anchors,
            ),
            plan_status,
        )

    from .data import liveness
    from .data.cleanup import (
        build_plan,
        execute_orphan_removals,
        execute_session_removals,
        prune_sessions,
        session_removal_anchors,
    )
    from .data.sessions import scan_result

    # One shared fetch feeds the frozen plan — the SAME assembly build_plan's
    # other callers (the Sessions view, build_world_snapshot) use, so the CLI
    # header and any future TUI parity stay derived from one source (删除 ⊆ 预览
    # still holds: execute_* revalidate against fresh data at apply time).
    inputs = liveness.liveness_inputs()
    if not inputs.complete:
        for liveness_issue in inputs.issues:
            where = f" ({liveness_issue.path})" if liveness_issue.path else ""
            print(
                "Refused: liveness evidence is incomplete: "
                f"{liveness_issue.source}{where}: {liveness_issue.detail}",
                file=sys.stderr,
            )
        return 1
    transcript_scan = scan_result(inputs)
    if not transcript_scan.complete:
        for transcript_issue in transcript_scan.issues:
            print(
                "Refused: transcript evidence is incomplete: "
                f"{transcript_issue.source} ({transcript_issue.path}): "
                f"{transcript_issue.detail}",
                file=sys.stderr,
            )
        return 1
    sessions = list(transcript_scan.sessions)
    plan = build_plan(sessions, inputs)
    counts = plan.counts()
    print(
        f"Total: {len(sessions)}  Prunable empty: {counts['empty']}  "
        f"short(<=2): {counts['short']}  Orphan dirs: {counts['orphan_dirs']}  "
        f"Zombie files: {counts['zombie_procs']}  Aged: {counts['aged_entries']}"
    )
    plan_status = _print_cleanup_issues(plan.issues)

    if args.sweep_orphans:
        orphans = plan.orphan_entries
        print(f"Would sweep {len(orphans)} orphan artifact dir(s)")
        if not args.apply:
            print("Dry run. Add --apply to execute.")
            return plan_status
        # Deletes AT MOST the listed entries, revalidated against fresh
        # protection data (删除 ⊆ 预览 — same executor as the TUI). The public
        # executor owns both fresh liveness and fresh transcript evidence.
        result = execute_orphan_removals(
            list(orphans),
            anchors=plan.orphan_anchors,
        )
        status = _print_cleanup_execution(
            result,
            success="Swept {n} orphan dir(s).",
            subject="orphan dir(s)",
        )
        return max(status, plan_status)

    if args.sweep_zombies:
        return max(
            _cmd_prune_zombies(args, list(plan.zombie_pids), plan.zombie_anchors),
            plan_status,
        )

    if args.sweep_aged:
        return max(
            _cmd_prune_aged(args, list(plan.aged_entries), plan.aged_anchors),
            plan_status,
        )

    targets = prune_sessions(
        sessions,
        max_prompts=args.max_prompts,
        evidence=inputs,
    )
    try:
        anchors = session_removal_anchors(targets)
    except OSError as exc:
        print(f"Refused: cannot establish removal anchors: {exc}", file=sys.stderr)
        return 1
    print(f"Would prune {len(targets)} session(s) (<={args.max_prompts} prompts)")

    if not args.apply:
        print("Dry run. Add --apply to execute.")
        return plan_status

    result = execute_session_removals(targets, anchors=anchors)
    status = _print_cleanup_execution(
        result,
        success="Pruned {n} session(s).",
        subject="session(s)",
    )
    return max(status, plan_status)


def _cmd_prune_zombies(
    args: Namespace,
    zombies: list[int] | None = None,
    anchors: Mapping[int, RemovalAnchor] | None = None,
) -> int:
    """Preview one typed generation; revalidate again only before mutation."""
    from .data import liveness
    from .data.cleanup import execute_zombie_removals, select_zombie_pids

    if zombies is None:
        evidence = liveness.liveness_inputs()
        if not evidence.complete:
            for issue in evidence.issues:
                where = f" ({issue.path})" if issue.path else ""
                print(
                    "Refused: liveness evidence is incomplete: "
                    f"{issue.source}{where}: {issue.detail}",
                    file=sys.stderr,
                )
            return 1
        zombies = select_zombie_pids(evidence.session_procs, evidence.cur)
    print(f"Would sweep {len(zombies)} zombie session file(s)")
    if not args.apply:
        print("Dry run. Add --apply to execute.")
        return 0
    result = execute_zombie_removals(zombies, anchors=anchors)
    return _print_cleanup_execution(
        result,
        success="Swept {n} zombie session file(s).",
        subject="zombie session file(s)",
    )


def _cmd_prune_aged(
    args: Namespace,
    aged: list[str],
    anchors: Mapping[str, RemovalAnchor],
) -> int:
    """Strategy B age sweep of time/global-keyed dirs (R7.2) via the CLI.

    The age sweep is mtime-only and session-agnostic, so (unlike the zombie
    sweep) it is not gated on `/proc`.
    """
    from .config import cfg
    from .data.cleanup import execute_aged_removals

    print(
        f"Would sweep {len(aged)} aged entr(y/ies) older than {cfg.cleanup_age_days}d"
    )
    if not args.apply:
        print("Dry run. Add --apply to execute.")
        return 0
    result = execute_aged_removals(aged, anchors=anchors)
    return _print_cleanup_execution(
        result,
        success="Swept {n} aged entr(y/ies).",
        subject="aged entr(y/ies)",
    )


def _cmd_resume(args: Namespace) -> int:
    from .actions.resume_list import render
    from .data import liveness
    from .data.sessions import scan_result

    inputs = liveness.liveness_inputs()
    if not inputs.complete:
        for liveness_issue in inputs.issues:
            where = f" ({liveness_issue.path})" if liveness_issue.path else ""
            print(
                "Refused: liveness evidence is incomplete: "
                f"{liveness_issue.source}{where}: {liveness_issue.detail}",
                file=sys.stderr,
            )
        return 1

    transcript_scan = scan_result(inputs)
    if not transcript_scan.complete:
        for transcript_issue in transcript_scan.issues:
            print(
                "Refused: transcript inventory is incomplete: "
                f"{transcript_issue.source} ({transcript_issue.path}): "
                f"{transcript_issue.detail}",
                file=sys.stderr,
            )
        return 1

    render_result = render(
        list(transcript_scan.sessions),
        keyword=args.keyword,
        page=args.page,
        limit=args.limit,
        all_pages=args.all_pages,
    )
    if not render_result.complete:
        for render_issue in render_result.issues:
            print(
                "Refused: transcript body search is incomplete: "
                f"{render_issue.source} ({render_issue.path}): "
                f"{render_issue.detail}",
                file=sys.stderr,
            )
        return 1

    print(render_result.text)
    return 0


def _cmd_skill(args: Namespace) -> int:
    from .actions import skill_ops

    try:
        if args.skill_command == "install":
            ok, msg = skill_ops.install(force=args.force)
        elif args.skill_command == "uninstall":
            ok, msg = skill_ops.uninstall()
        else:
            print("Usage: csctl skill <install|uninstall>", file=sys.stderr)
            return 2
    except (OSError, UnicodeError) as exc:
        print(f"Skill operation failed: {exc}", file=sys.stderr)
        return 1
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def _cmd_agents(args: Namespace) -> int:
    from .data.liveness import liveness_inputs

    inputs = liveness_inputs()
    jobs = inputs.agent_jobs
    if not jobs:
        print("No background agents found.")
    else:
        for job in jobs:
            state = "live" if job.host_alive else (job.state or "settled")
            tempo = job.tempo or "-"
            name = job.name or job.short
            print(f"  {job.short}  [{state}]  tempo={tempo}  {name}  {job.cwd}")
    for issue in inputs.issues:
        where = f" ({issue.path})" if issue.path else ""
        print(
            f"Warning: agent inventory is partial: {issue.source}{where}: "
            f"{issue.detail}",
            file=sys.stderr,
        )
    return int(not inputs.complete)


def _cmd_env(args: Namespace) -> int:
    from .data import environments, liveness, rc

    # Scan RC servers so the env_* namespace is covered too (it has no state
    # file — only a running server references it). The whole observe → upsert →
    # classify pipeline (and its ordering invariant) lives in reconcile.
    evidence = liveness.liveness_inputs()
    server_scan = rc.scan_servers_result()
    recon = environments.reconcile(
        evidence,
        server_scan.servers,
        inventory_issues=server_scan.issues,
    )

    current_label = " (partial)" if not recon.evidence_complete else ""
    print(f"Current bridge environments{current_label}: {len(recon.current)}")
    for e in recon.current:
        print(f"  {e.env_id}  sid={e.bound_sid or '-'}")

    if recon.evidence_complete:
        history_note = (
            " (ledger history incomplete)" if not recon.ledger_history_complete else ""
        )
        print(
            "Orphan environments (delete manually on claude.ai/code): "
            f"{len(recon.orphans)}{history_note}",
        )
        for e in recon.orphans:
            print(f"  {e.env_id}  sid={e.bound_sid or '-'}")
    else:
        print("Orphan environments: unavailable (partial liveness evidence)")

    for liveness_issue in recon.liveness_issues:
        where = f" ({liveness_issue.path})" if liveness_issue.path else ""
        print(
            "Warning: environment inventory is partial: "
            f"{liveness_issue.source}{where}: {liveness_issue.detail}",
            file=sys.stderr,
        )
    for inventory_issue in recon.inventory_issues:
        where = f" ({inventory_issue.path})" if inventory_issue.path else ""
        print(
            "Warning: environment inventory is partial: "
            f"{inventory_issue.source}{where}: {inventory_issue.detail}",
            file=sys.stderr,
        )
    for warning in recon.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    print(
        "Note: csctl cannot deregister cloud environments; "
        "the orphan list is inherently incomplete "
        "(environments minted while csctl was not running are not tracked)."
    )
    return int(not recon.success)


def _cmd_tui(args: Namespace) -> int:
    from .actions.session_ops import ExitIntent
    from .app import App

    result = App().run()

    # The intent finalizes itself outside the urwid loop (it may exec-replace
    # csctl — resume/attach) and returns the process status on failure.
    if isinstance(result, ExitIntent):
        return result.run()
    return 0


def handle_prune(
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    return run_with_streams(
        _cmd_prune,
        args,
        stdout=stdout,
        stderr=stderr,
    )


def handle_resume(
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    return run_with_streams(
        _cmd_resume,
        args,
        stdout=stdout,
        stderr=stderr,
    )


def handle_skill_install(
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    return run_with_streams(
        _cmd_skill,
        args,
        stdout=stdout,
        stderr=stderr,
    )


handle_skill_uninstall = handle_skill_install


def handle_agents(
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    return run_with_streams(
        _cmd_agents,
        args,
        stdout=stdout,
        stderr=stderr,
    )


def handle_env(
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    return run_with_streams(
        _cmd_env,
        args,
        stdout=stdout,
        stderr=stderr,
    )


def handle_tui(
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    return run_with_streams(
        _cmd_tui,
        args,
        stdout=stdout,
        stderr=stderr,
    )
