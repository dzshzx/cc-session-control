"""Remote Control command handlers for the ``csctl`` CLI."""

from __future__ import annotations

import os
import sys
from argparse import Namespace
from typing import TextIO

from .cli_streams import run_with_streams


def _run_rc(args: Namespace) -> int:
    from .data import rc
    from .models import TrustDecision

    if not args.rc_command:
        print(
            "Usage: csctl rc <status|add|rm|up|stop|list>",
            file=sys.stderr,
        )
        return 2

    sub = args.rc_command
    if sub == "status":
        from .data.sessions import scan as scan_sessions

        scan_result = rc.scan_result()
        projects = rc.order_by_activity(
            scan_result.projects,
            scan_sessions(),
        )
        if not scan_result.settings.available:
            print(
                "Project settings unavailable: "
                f"{scan_result.settings.state.value}"
                f"{': ' + scan_result.settings.detail if scan_result.settings.detail else ''}",
                file=sys.stderr,
            )
        for project in projects:
            icon = {
                "running": "[running]",
                "dead": "[dead   ]",
                "stopped": "[stopped]",
            }.get(project.status, project.status)
            auto = "auto" if project.auto_start else "    "
            missing = "" if project.dir_exists else "  (directory missing)"
            print(
                f"  {icon} {auto}  {project.name}  {project.directory}{missing}",
            )
        return 0 if scan_result.settings.available else 1

    if sub == "add":
        path = os.path.abspath(args.project)
        if not os.path.isdir(path):
            print(f"No such directory: {path}", file=sys.stderr)
            return 1
        trust = rc.project_trust(path)
        if trust.decision is TrustDecision.UNAVAILABLE:
            print(
                "Project settings unavailable: "
                f"{trust.settings.state.value}"
                f"{': ' + trust.settings.detail if trust.settings.detail else ''}"
                " — refusing to start",
                file=sys.stderr,
            )
            return 1
        if trust.decision is TrustDecision.UNTRUSTED:
            print(
                f"Not trusted: {path} — run 'claude' in that directory "
                "first to accept the trust dialog",
                file=sys.stderr,
            )
            return 1
        try:
            rc.list_add(path)
        except (OSError, UnicodeError) as exc:
            print(
                f"Failed to add project to the enabled list: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"Added to list: {path}")
        start_result = rc.start_one_result(path)
        if start_result.state is rc.StartState.STARTED:
            print(f"Started RC server for {path}")
            return 0
        if start_result.state is rc.StartState.TRUST_UNAVAILABLE:
            print(
                "Project settings became unavailable — refusing to start",
                file=sys.stderr,
            )
        elif start_result.state is rc.StartState.UNTRUSTED:
            print(
                "Project is no longer trusted — refusing to start",
                file=sys.stderr,
            )
        else:
            print(
                f"RC server was not started: {start_result.state.value}",
                file=sys.stderr,
            )
        return 1

    if sub == "rm":
        path = os.path.abspath(args.project)
        try:
            remove_result = rc.remove_one_result(path)
        except (OSError, UnicodeError) as exc:
            print(f"Failed to remove {path}: {exc}", file=sys.stderr)
            return 1
        if remove_result.stop.state is rc.StopState.TMUX_FAILED:
            prefix = (
                "Removed from the enabled list, but "
                if remove_result.list_removed
                else ""
            )
            print(
                f"{prefix}failed to stop the RC window: {path}",
                file=sys.stderr,
            )
            return 1
        if remove_result.stop.state is rc.StopState.STOPPED:
            print(f"Removed and stopped: {path}")
            return 0
        if remove_result.list_removed:
            print(f"Removed from the enabled list (not running): {path}")
            return 0
        print(f"Not enabled or running: {path}", file=sys.stderr)
        return 1

    if sub == "up":
        try:
            enabled = rc.list_enabled()
        except (OSError, UnicodeError) as exc:
            print(f"Failed to read the enabled list: {exc}", file=sys.stderr)
            return 1
        if not enabled:
            print("List is empty")
            return 0
        batch_result = rc.start_many_result(enabled)
        print(f"Started {batch_result.started} project(s)")
        if batch_result.unavailable:
            print(
                "Project settings unavailable; refused "
                f"{batch_result.unavailable} project(s)",
                file=sys.stderr,
            )
        if batch_result.untrusted:
            print(
                f"Not trusted; refused {batch_result.untrusted} project(s)",
                file=sys.stderr,
            )
        if batch_result.failed:
            print(
                f"Failed to start {batch_result.failed} project(s)",
                file=sys.stderr,
            )
        return int(
            bool(
                batch_result.unavailable
                or batch_result.untrusted
                or batch_result.failed
            )
        )

    if sub == "stop":
        if args.target == "all":
            if rc.stop_all():
                print("Stopped all")
                return 0
            print(
                "Failed to stop all RC servers "
                "(the configured tmux session may be unavailable)",
                file=sys.stderr,
            )
            return 1
        path = os.path.abspath(args.target)
        stop_result = rc.stop_one_result(path)
        if stop_result.state is rc.StopState.STOPPED:
            print(f"Stopped {path}")
            return 0
        if stop_result.state is rc.StopState.NOT_RUNNING:
            print(f"Not running: {path}", file=sys.stderr)
            return 1
        print(
            f"Failed to stop {path} (tmux unavailable or returned nonzero)",
            file=sys.stderr,
        )
        return 1

    if sub == "list":
        try:
            enabled = rc.list_enabled()
        except (OSError, UnicodeError) as exc:
            print(f"Failed to read the enabled list: {exc}", file=sys.stderr)
            return 1
        for name in enabled:
            print(name)
        return 0

    print(f"Unknown rc command: {sub}", file=sys.stderr)
    return 2


def handle_rc(
    args: Namespace,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Render one parsed RC leaf command and return its process status."""
    return run_with_streams(
        _run_rc,
        args,
        stdout=stdout,
        stderr=stderr,
    )


handle_status = handle_rc
handle_add = handle_rc
handle_rm = handle_rc
handle_up = handle_rc
handle_stop = handle_rc
handle_list = handle_rc
