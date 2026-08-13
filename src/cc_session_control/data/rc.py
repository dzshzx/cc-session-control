"""Manage evidence-tier membership projects and their RC surface via tmux.

Names are display-only; tmux and settings joins use absolute paths.
Membership itself (which directories are projects at all) lives in
`data.membership` (ADR-0007); this module owns the RC join: window status,
per-project settings, spawn modes, and the start/stop verbs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence

from ..config import cfg
from ..models import (
    InventoryIssue,
    RCProject,
    RCServer,
    Session,
    Status,
    TrustDecision,
    effective_trust_decision,
)
from . import proc, providers, rc_outcomes, tmux
from .curation import read_curation
from .membership import compute_membership
from .project_settings import (
    ProjectSettingsResult,
    read_project_settings,
    read_rc_at_startup,
)
from .rc_outcomes import (
    ProjectTrustResult,
    RCScanResult,
    RCServerScanResult,
    StartResult,
    StartState,
    StopAllResult,
    StopResult,
    StopState,
)


def _read_projects() -> ProjectSettingsResult:
    """Typed single source for ``~/.claude.json`` project metadata."""

    return read_project_settings(cfg.claude_json)


def project_trust(path: str) -> ProjectTrustResult:
    """Effective trust plus typed settings evidence."""

    settings = _read_projects()
    projects = settings.projects if settings.available else None
    return ProjectTrustResult(effective_trust_decision(path, projects), settings)


def _basename(path: str) -> str:
    """Display name derived from the path — NEVER an identity key."""
    return os.path.basename(path.rstrip("/")) or path


# RC-scoped delegates keep `cfg.rc_session` out of the generic tmux adapter.


def _tmux_window_inventory() -> tmux.WindowInventory:
    """Typed RC-session window inventory used by production decisions."""

    return tmux.list_windows_inventory(cfg.rc_session)


def _window_for_inventory(
    path: str,
    inventory: tmux.WindowInventory,
) -> tmux.TmuxWindow | None:
    norm = os.path.normpath(path)
    return next(
        (
            window
            for window in inventory.records
            if window.path and os.path.normpath(window.path) == norm
        ),
        None,
    )


def scan_result(
    *,
    window_inventory: tmux.WindowInventory | None = None,
    sessions: Sequence[Session] = (),
) -> RCScanResult:
    # ONE claude.json load feeds membership, trust flags and spawn modes —
    # no per-project re-parse.
    settings = _read_projects()
    projects_map = settings.projects
    inventory = (
        _tmux_window_inventory() if window_inventory is None else window_inventory
    )
    by_path = {os.path.normpath(w.path): w for w in inventory.records if w.path}
    trust_scan = providers.scan_trusted_dirs()
    curation = read_curation(cfg.curation_file)
    entries = compute_membership(
        claude_projects=projects_map if settings.available else None,
        provider_trust=trust_scan.directories,
        sessions=sessions,
        pinned=curation.pinned,
        hidden=curation.hidden,
        window_paths=by_path,
        now=time.time(),
    )
    raw_by_norm = {os.path.normpath(key): key for key in projects_map}

    result: list[RCProject] = []
    for entry in entries:
        path = entry.directory
        win = by_path.get(path)
        if win is not None:
            status: Status = "dead" if win.dead else "running"
        elif not inventory.complete:
            status = "unknown"
        else:
            status = "stopped"
        raw_key = raw_by_norm.get(path)
        project_entry = projects_map.get(raw_key) if raw_key is not None else None
        spawn = (
            project_entry.get("remoteControlSpawnMode")
            if isinstance(project_entry, Mapping)
            else None
        )
        decision = effective_trust_decision(
            path,
            projects_map if settings.available else None,
        )
        result.append(
            RCProject(
                name=_basename(path),
                directory=path,
                trust_decision=decision,
                status=status,
                rc_at_startup_setting=read_rc_at_startup(path),
                spawn_mode=str(spawn) if spawn else None,
                dir_exists=entry.dir_exists,
                pinned=entry.pinned,
                hidden=entry.hidden,
                trusted_by=entry.trusted_by,
                observed_by=entry.observed_by,
            )
        )
    membership_issues = list(trust_scan.issues)
    if not curation.available:
        membership_issues.append(
            InventoryIssue(
                "curation",
                os.fspath(cfg.curation_file),
                curation.detail or curation.state.value,
            )
        )
    return RCScanResult(
        result,
        settings,
        inventory.issues,
        tuple(membership_issues),
    )


order_by_activity = rc_outcomes.order_by_activity


def scan_servers_result(
    *,
    window_inventory: tmux.WindowInventory | None = None,
    proc_inventory: proc.ProcRCInventory | None = None,
) -> RCServerScanResult:
    """All project RC servers: managed (csctl tmux) ∪ external (/proc) — R5/D5.

    Managed = tmux windows in `cfg.rc_session` (their pane pid IS the server
    pid); external = `/proc`-discovered `claude remote-control --name` processes
    NOT owned by a managed pane. External servers are READ-ONLY (no
    takeover/restart — review gate; sustains the "no auto-restart RC" rule).

    The lower tmux and proc adapters own expected external failures; parser and
    programming failures stay observable.
    """
    window_scan = (
        _tmux_window_inventory() if window_inventory is None else window_inventory
    )
    process_scan = (
        proc.scan_rc_server_inventory() if proc_inventory is None else proc_inventory
    )
    windows = window_scan.records
    discovered = process_scan.records

    by_pid = {p.pid: p for p in discovered}
    managed_pid_set = {w.pid for w in windows if w.pid}

    servers: list[RCServer] = []
    # Managed windows first — tmux is the authority for "managed". Addressed
    # by the server-unique window id, never by the (collision-prone) name.
    for w in windows:
        status: Status = "dead" if w.dead else "running"
        found = by_pid.get(w.pid) if w.pid else None
        servers.append(
            RCServer(
                name=found.name if found else w.name,
                cwd=found.cwd if found else w.path,
                managed=True,
                pid=w.pid or None,
                status=status,
            )
        )

    # External — discovered procs not owned by any managed pane.
    for p in discovered if window_scan.complete else ():
        if p.pid in managed_pid_set:
            continue
        servers.append(
            RCServer(
                name=p.name,
                cwd=p.cwd,
                managed=False,
                pid=p.pid or None,
                status="running",
            )
        )

    issues = (
        *window_scan.issues,
        *rc_outcomes.proc_inventory_issues(process_scan),
    )
    return RCServerScanResult(tuple(servers), issues)


def _start_one_with_trust(
    path: str,
    decision: TrustDecision,
    *,
    window_inventory: tmux.WindowInventory | None = None,
) -> StartResult:
    if not os.path.isdir(path):
        return StartResult(StartState.NOT_DIRECTORY, path)
    if decision is TrustDecision.UNAVAILABLE:
        return StartResult(StartState.TRUST_UNAVAILABLE, path)
    if decision is TrustDecision.UNTRUSTED:
        return StartResult(StartState.UNTRUSTED, path)
    inventory = (
        _tmux_window_inventory() if window_inventory is None else window_inventory
    )
    issues = inventory.issues
    if issues:
        return StartResult(
            StartState.INVENTORY_UNAVAILABLE,
            path,
            rc_outcomes.format_inventory_issues(issues),
            issues,
        )
    win = _window_for_inventory(path, inventory)
    if win is not None:
        if not win.dead:
            return StartResult(StartState.ALREADY_RUNNING, path)
        stop_result = stop_one_result(path, window_inventory=inventory)
        if stop_result.state is StopState.FAILED:
            return StartResult(
                StartState.STOP_FAILED,
                path,
                stop_result.detail,
                stop_result.issues,
            )

    cmd = rc_outcomes.remote_control_command(path, _basename(path))
    create_result = tmux.run_in_tmux_result(
        cfg.rc_session, tmux.project_name_for(path), cmd
    )
    if not create_result.success:
        return rc_outcomes.start_from_tmux(StartState.TMUX_FAILED, path, create_result)
    target = create_result.target
    if target is None:
        raise AssertionError("successful tmux create must carry a target")
    # Declare the window's project — the collision-safe join key `scan` and
    # `stop_one_result` read back. Until this lands, `pane_current_path` (the `cd`
    # above) covers the same join, so a mid-spawn scan still matches.
    metadata_result = tmux.set_window_option_result(target, "@csctl_path", path)
    state = (
        StartState.STARTED if metadata_result.success else StartState.METADATA_FAILED
    )
    return rc_outcomes.start_from_tmux(state, path, metadata_result)


def start_one_result(path: str) -> StartResult:
    """Start one RC server with tri-state trust evidence."""

    return _start_one_with_trust(path, project_trust(path).decision)


def stop_one_result(
    path: str,
    *,
    window_inventory: tmux.WindowInventory | None = None,
) -> StopResult:
    inventory = (
        _tmux_window_inventory() if window_inventory is None else window_inventory
    )
    issues = inventory.issues
    if issues:
        return StopResult(
            StopState.FAILED,
            path,
            rc_outcomes.format_inventory_issues(issues),
            issues,
        )
    win = _window_for_inventory(path, inventory)
    if win is None:
        return StopResult(StopState.NOT_RUNNING, path)
    kill_result = tmux.kill_window_result(win.wid)
    if kill_result.state is tmux.KillState.KILLED:
        return StopResult(StopState.STOPPED, path)
    if kill_result.state is tmux.KillState.TARGET_NOT_FOUND:
        return StopResult(StopState.NOT_RUNNING, path, kill_result.detail)
    return StopResult(StopState.FAILED, path, kill_result.detail)


def stop_all_result() -> StopAllResult:
    """Stop the configured RC tmux session without conflating absence/failure."""

    kill_result = tmux.kill_session_result(cfg.rc_session)
    if kill_result.state is tmux.KillState.KILLED:
        return StopAllResult(StopState.STOPPED, cfg.rc_session)
    if kill_result.state is tmux.KillState.TARGET_NOT_FOUND:
        return StopAllResult(
            StopState.NOT_RUNNING,
            cfg.rc_session,
            kill_result.detail,
        )
    return StopAllResult(StopState.FAILED, cfg.rc_session, kill_result.detail)
