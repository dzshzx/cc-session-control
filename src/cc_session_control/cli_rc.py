"""Remote Control command handlers for the ``csctl`` CLI.

Each ``_cmd_*`` function implements exactly one ``csctl rc <leaf>`` sub-
subcommand and is bound directly to its argparse leaf parser via
``set_defaults(handler=...)`` in ``cli.py``. argparse's subparsers already
resolve ``rc_command`` to exactly one of these functions once, so there is no
second re-dispatch ladder here.
"""

from __future__ import annotations

import os
import sys
from argparse import Namespace

from .data.rc_enabled import EnabledListResult


def _enabled_list_failure[Value](result: EnabledListResult[Value]) -> str:
    stage = result.stage.value if result.stage is not None else "unknown"
    committed = str(result.committed).lower()
    suffix = "; list change committed before failure" if result.committed else ""
    return f"stage={stage}; committed={committed}; detail={result.detail}{suffix}"


def _cmd_status(args: Namespace) -> int:
    from .data import liveness, rc, rc_outcomes
    from .data.sessions import scan_result as scan_sessions_result

    liveness_snapshot = liveness.liveness_inputs()
    rc_scan_result = rc.scan_result()
    transcript_scan_result = scan_sessions_result(liveness_snapshot)
    projects = rc.order_by_activity(
        rc_scan_result.projects,
        list(transcript_scan_result.sessions),
    )
    if not rc_scan_result.settings.available:
        print(
            "Project settings unavailable: "
            f"{rc_scan_result.settings.state.value}"
            f"{': ' + rc_scan_result.settings.detail if rc_scan_result.settings.detail else ''}",
            file=sys.stderr,
        )
    scan_enabled_list = rc_scan_result.enabled_list
    if scan_enabled_list is not None and not scan_enabled_list.success:
        print(
            "Warning: enabled list inventory is partial: "
            f"{_enabled_list_failure(scan_enabled_list)}",
            file=sys.stderr,
        )
    for liveness_issue in liveness_snapshot.issues:
        print(
            "Warning: liveness inventory is partial: "
            + rc_outcomes.format_inventory_issues((liveness_issue,)),
            file=sys.stderr,
        )
    for rc_issue in rc_scan_result.issues:
        print(
            "Warning: RC inventory is partial: "
            + rc_outcomes.format_inventory_issues((rc_issue,)),
            file=sys.stderr,
        )
    for transcript_issue in transcript_scan_result.issues:
        print(
            "Warning: transcript inventory is partial: "
            + rc_outcomes.format_inventory_issues((transcript_issue,)),
            file=sys.stderr,
        )
    setting_failure = False
    for project in projects:
        icon = {
            "running": "[running]",
            "dead": "[dead   ]",
            "stopped": "[stopped]",
            "unknown": "[unknown]",
        }.get(project.status, project.status)
        auto = "auto" if project.auto_start else "    "
        missing = "" if project.dir_exists else "  (directory missing)"
        print(
            f"  {icon} {auto}  {project.name}  {project.directory}{missing}",
        )
        setting = project.rc_at_startup_setting
        if not setting.available:
            setting_failure = True
            print(
                "Project remoteControlAtStartup unavailable: "
                f"project={project.directory}; "
                f"state={setting.state.value}; "
                f"source={setting.source}; "
                f"detail={setting.detail}",
                file=sys.stderr,
            )
    return (
        0
        if rc_scan_result.settings.available
        and rc_scan_result.complete
        and liveness_snapshot.complete
        and transcript_scan_result.complete
        and not setting_failure
        else 1
    )


def _cmd_add(args: Namespace) -> int:
    from .data import rc
    from .models import TrustDecision

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
    add_enabled_list = rc.list_add_result(path)
    if not add_enabled_list.success:
        print(
            "Failed to add project to the enabled list: "
            f"{_enabled_list_failure(add_enabled_list)}",
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
    elif start_result.state is rc.StartState.METADATA_FAILED:
        target = start_result.target or "unknown"
        detail = start_result.detail or "tmux metadata write failed"
        print(
            f"RC server target {target} was created, but metadata was not "
            f"written: {detail}",
            file=sys.stderr,
        )
    else:
        detail = f": {start_result.detail}" if start_result.detail else ""
        print(
            f"RC server was not started: {start_result.state.value}{detail}",
            file=sys.stderr,
        )
    return 1


def _cmd_rm(args: Namespace) -> int:
    from .data import rc

    path = os.path.abspath(args.project)
    remove_result = rc.remove_one_result(path)
    remove_enabled_list = remove_result.enabled_list
    if not remove_enabled_list.success:
        print(
            "Failed to remove project from the enabled list: "
            f"{_enabled_list_failure(remove_enabled_list)}",
            file=sys.stderr,
        )
        return 1
    stop_result = remove_result.stop
    if stop_result is None:
        raise AssertionError("successful enabled-list removal must attempt stop")
    if stop_result.state is rc.StopState.FAILED:
        prefix = (
            "Removed from the enabled list, but " if remove_result.list_removed else ""
        )
        detail = stop_result.detail or "tmux operation failed"
        print(
            f"{prefix}failed to stop the RC window: {path}: {detail}",
            file=sys.stderr,
        )
        return 1
    if stop_result.state is rc.StopState.STOPPED:
        print(f"Removed and stopped: {path}")
        return 0
    if remove_result.list_removed:
        print(f"Removed from the enabled list (not running): {path}")
        return 0
    print(f"Not enabled or running: {path}", file=sys.stderr)
    return 1


def _cmd_up(args: Namespace) -> int:
    from .data import rc

    batch_result = rc.start_all_listed_result()
    batch_enabled_list = batch_result.enabled_list
    if batch_enabled_list is not None and not batch_enabled_list.success:
        print(
            "Failed to read the enabled list: "
            f"{_enabled_list_failure(batch_enabled_list)}",
            file=sys.stderr,
        )
        return 1
    if (
        batch_enabled_list is not None
        and batch_enabled_list.success
        and batch_enabled_list.value == ()
    ):
        print("List is empty")
        return 0
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
        for result in batch_result.results:
            if result.state in {
                rc.StartState.STARTED,
                rc.StartState.TRUST_UNAVAILABLE,
                rc.StartState.UNTRUSTED,
            }:
                continue
            detail = result.detail or "no diagnostic detail"
            state = result.state.value
            if result.state is rc.StartState.METADATA_FAILED and result.target:
                state += f"; target {result.target} created"
            print(
                f"  {result.path} [{state}]: {detail}",
                file=sys.stderr,
            )
    return int(
        bool(batch_result.unavailable or batch_result.untrusted or batch_result.failed)
    )


def _cmd_stop(args: Namespace) -> int:
    from .data import rc

    if args.target == "all":
        stop_all_result = rc.stop_all_result()
        if stop_all_result.state is rc.StopState.STOPPED:
            print("Stopped all")
            return 0
        if stop_all_result.state is rc.StopState.NOT_RUNNING:
            print("No RC servers are running", file=sys.stderr)
            return 1
        detail = stop_all_result.detail or "tmux operation failed"
        print(
            f"Failed to stop all RC servers: {detail}",
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
    detail = stop_result.detail or "tmux operation failed"
    print(
        f"Failed to stop {path}: {detail}",
        file=sys.stderr,
    )
    return 1


def _cmd_list(args: Namespace) -> int:
    from .data import rc

    listed_enabled = rc.list_enabled_result()
    if not listed_enabled.success:
        print(
            f"Failed to read the enabled list: {_enabled_list_failure(listed_enabled)}",
            file=sys.stderr,
        )
        return 1
    if listed_enabled.value is None:
        raise AssertionError("successful enabled-list read must carry paths")
    for name in listed_enabled.value:
        print(name)
    return 0
