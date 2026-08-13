"""Command handlers and renderers for the ``csctl`` CLI."""

from __future__ import annotations

import sys
from argparse import Namespace


def _cmd_resume(args: Namespace) -> int:
    from .actions import session_ops
    from .actions.resume_list import render
    from .data import liveness, providers
    from .data.sessions import scan_result
    from .models import format_inventory_issues

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
                + format_inventory_issues((liveness_issue,)),
                file=sys.stderr,
            )
        return 1

    transcript_scan = scan_result(inputs)
    if not transcript_scan.complete:
        for transcript_issue in transcript_scan.issues:
            print(
                "Refused: transcript inventory is incomplete: "
                + format_inventory_issues((transcript_issue,)),
                file=sys.stderr,
            )
        return 1

    # Non-Claude providers join the same list (ADR-0005); their source issues
    # degrade to warnings — a broken codex index must not block Claude resumes.
    provider_rows, provider_issues = providers.scan_non_claude(inputs.cur)
    for provider_issue in provider_issues:
        print(
            "Warning: provider inventory is partial: "
            + format_inventory_issues((provider_issue,)),
            file=sys.stderr,
        )
    rows = providers.merge_sessions(transcript_scan.sessions, provider_rows)

    render_result = render(
        list(rows),
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


def cmd_kimi_hook(argv: list[str]) -> int:
    """Internal kimi hook endpoint (SessionStart/SessionEnd → runtime
    registry); the event payload arrives on stdin. Exit codes are the
    contract of `actions/kimi_hook.run_hook` (2 here = stray arguments)."""
    if argv:
        print("usage: csctl _kimi-hook  (event payload on stdin)", file=sys.stderr)
        return 2
    from .actions import kimi_hook

    return kimi_hook.run_hook(sys.stdin.read())


def _cmd_tui(args: Namespace) -> int:
    from .actions.session_ops import ExitIntent
    from .app import App

    result = App().run()

    # The intent finalizes itself outside the urwid loop (it may exec-replace
    # csctl — resume/attach) and returns the process status on failure.
    if isinstance(result, ExitIntent):
        return result.run()
    return 0
