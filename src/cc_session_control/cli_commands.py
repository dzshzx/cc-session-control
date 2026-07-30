"""Command handlers and renderers for the ``csctl`` CLI."""

from __future__ import annotations

import sys
from argparse import Namespace


def _cmd_resume(args: Namespace) -> int:
    from .actions import session_ops
    from .actions.resume_list import render
    from .data import liveness, rc_outcomes
    from .data.sessions import scan_result

    take_over_sid: str | None = getattr(args, "take_over", None)
    if take_over_sid is not None:
        try:
            outcome = session_ops.do_resume_sid_result(take_over_sid)
        except OSError as exc:
            print(
                f"Failed to take over session {take_over_sid}: {exc}",
                file=sys.stderr,
            )
            return 1
        if not outcome.success:
            detail = f": {outcome.detail}" if outcome.detail else ""
            print(
                f"Refused: take over did not occur for session "
                f"{take_over_sid!r}{detail}.",
                file=sys.stderr,
            )
            return 1
        return 0

    inputs = liveness.liveness_inputs()
    if not inputs.complete:
        for liveness_issue in inputs.issues:
            print(
                "Refused: liveness evidence is incomplete: "
                + rc_outcomes.format_inventory_issues((liveness_issue,)),
                file=sys.stderr,
            )
        return 1

    transcript_scan = scan_result(inputs)
    if not transcript_scan.complete:
        for transcript_issue in transcript_scan.issues:
            print(
                "Refused: transcript inventory is incomplete: "
                + rc_outcomes.format_inventory_issues((transcript_issue,)),
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


def _cmd_agents(args: Namespace) -> int:
    from .data import rc_outcomes
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
        print(
            "Warning: agent inventory is partial: "
            + rc_outcomes.format_inventory_issues((issue,)),
            file=sys.stderr,
        )
    return int(not inputs.complete)


def _cmd_tui(args: Namespace) -> int:
    from .actions.session_ops import ExitIntent
    from .app import App

    result = App().run()

    # The intent finalizes itself outside the urwid loop (it may exec-replace
    # csctl — resume/attach) and returns the process status on failure.
    if isinstance(result, ExitIntent):
        return result.run()
    return 0
