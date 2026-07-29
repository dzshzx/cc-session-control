"""Manage path-keyed Claude Code Remote Control projects via tmux.

Names are display-only; enabled-list, tmux, and settings joins use absolute paths.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import replace

from ..config import cfg
from ..models import (
    RCProject,
    RCServer,
    Status,
    TrustDecision,
    effective_trust_decision,
)
from . import proc, rc_environment, rc_outcomes, tmux
from .project_settings import (
    ProjectSettingsResult,
    SettingWriteResult,
    read_project_settings,
    read_rc_at_startup,
    write_rc_at_startup,
)
from .rc_enabled import EnabledListResult, EnabledListStore
from .rc_outcomes import (
    ProjectTrustResult,
    RCScanResult,
    RCServerScanResult,
    RemoveResult,
    StartManyResult,
    StartResult,
    StartState,
    StopAllResult,
    StopResult,
    StopState,
)

_environment_ids = rc_environment.EnvironmentIdCache()


def _legacy_workspace_root() -> str:
    """FROZEN pre-0.7.3 workspace detection for rc-enabled migration only."""
    env = os.environ.get("CSCTL_WORKSPACE")
    if env:
        return env
    default = os.path.join(os.path.expanduser("~"), "workspace")
    if os.path.isdir(default):
        return default
    try:
        dirs = [key for key in _load_projects() if isinstance(key, str) and "/" in key]
        if dirs:
            common = os.path.commonpath(dirs)
            if os.path.isdir(common) and common != os.path.expanduser("~"):
                return common
    except (OSError, ValueError):
        pass
    return os.getcwd()


def _enabled_store() -> EnabledListStore:
    return EnabledListStore(cfg.rc_list, _legacy_workspace_root)


def list_enabled_result() -> EnabledListResult[tuple[str, ...]]:
    return _enabled_store().list_result()


def list_add_result(path: str) -> EnabledListResult[bool]:
    return _enabled_store().add_result(path)


def list_rm_result(path: str) -> EnabledListResult[bool]:
    return _enabled_store().remove_result(path)


def toggle_autostart_result(path: str) -> EnabledListResult[bool]:
    return _enabled_store().toggle_result(path)


def _load_projects() -> Mapping[str, Mapping[str, object]]:
    """Compatibility map-only reader; typed callers use ``_read_projects``.

    Failures become an empty map only at this legacy boundary. Safety decisions
    never call it, so they retain ``UNAVAILABLE``.
    """
    return _read_projects().projects


def _read_projects() -> ProjectSettingsResult:
    """Typed single source for ``~/.claude.json`` project metadata."""

    return read_project_settings(cfg.claude_json)


def _trusted_in(projects: Mapping[str, object]) -> set[str]:
    """Effectively-trusted absolute-path keys of a claude.json projects map."""
    return {
        key
        for key in projects
        if isinstance(key, str)
        and key.startswith("/")
        and effective_trust_decision(key, projects) is TrustDecision.TRUSTED
    }


# Temp roots are working space, not projects; this affects membership, not trust.
_TEMP_ROOTS = frozenset(
    os.path.normpath(p) for p in (tempfile.gettempdir(), "/tmp", "/var/tmp")
)


def _is_temp_path(path: str) -> bool:
    """PURE: is `path` a platform temp root, or beneath one?

    Same segment-boundary matching as `models.effective_trust` (normpath
    only; `/tmpfoo` is not under `/tmp`).
    """
    target = os.path.normpath(path)
    for root in _TEMP_ROOTS:
        if target == root or target.startswith(root.rstrip("/") + "/"):
            return True
    return False


def project_trust(path: str) -> ProjectTrustResult:
    """Effective trust plus typed settings evidence."""

    settings = _read_projects()
    projects = settings.projects if settings.available else None
    return ProjectTrustResult(effective_trust_decision(path, projects), settings)


def trust_decision(path: str) -> TrustDecision:
    """Compatibility decision-only view of ``project_trust``."""

    return project_trust(path).decision


def _basename(path: str) -> str:
    """Display name derived from the path — NEVER an identity key."""
    return os.path.basename(path.rstrip("/")) or path


# RC-scoped delegates keep `cfg.rc_session` out of the generic tmux adapter.


def _tmux_window_inventory() -> tmux.WindowInventory:
    """Typed RC-session window inventory used by production decisions."""

    return tmux.list_windows_inventory(cfg.rc_session)


def _tmux_capture_pane_result(target: str) -> tmux.PaneCaptureResult:
    """Typed pane capture used by production RC inventory."""

    return tmux.capture_pane_result(target)


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


def set_rc_at_startup(
    directory: str,
    value: bool | None,
) -> SettingWriteResult:
    """Typed compatibility name for the atomic project-settings writer."""

    return write_rc_at_startup(directory, value)


def scan_result(
    *,
    window_inventory: tmux.WindowInventory | None = None,
) -> RCScanResult:
    # ONE claude.json load feeds membership, trust flags and spawn modes —
    # no per-project re-parse.
    settings = _read_projects()
    projects_map = settings.projects
    trusted = _trusted_in(projects_map)
    enabled_result = list_enabled_result()
    enabled = set(enabled_result.value or ())
    inventory = (
        _tmux_window_inventory() if window_inventory is None else window_inventory
    )
    by_path = {os.path.normpath(w.path): w for w in inventory.records if w.path}
    candidates = trusted | enabled
    if not enabled_result.success:
        candidates |= {window.path for window in inventory.records if window.path}

    result: list[RCProject] = []
    for path in sorted(candidates):
        win = by_path.get(os.path.normpath(path))
        dir_exists = os.path.isdir(path)
        if not dir_exists and path not in enabled and win is None:
            # Pure trust residue: the directory is gone and only claude's own
            # trust record (~/.claude.json) still references it. csctl can't
            # act on it (no start, and it never edits claude's files), so it
            # is dropped instead of rendered as a ✖ 缺失 row. Missing-dir
            # projects that ARE actionable (in the autostart list, or with a
            # live/dead tmux window) stay listed.
            continue
        if _is_temp_path(path) and path not in enabled and win is None:
            # Temp dirs reached via trust discovery alone are dropped —
            # same escape hatch as above: explicitly actionable entries
            # (autostart list, existing rc window) stay listed.
            continue
        if win is not None:
            status: Status = "dead" if win.dead else "running"
        elif not inventory.complete:
            status = "unknown"
        else:
            status = "stopped"
        entry = projects_map.get(path)
        spawn = (
            entry.get("remoteControlSpawnMode") if isinstance(entry, Mapping) else None
        )
        decision = effective_trust_decision(
            path,
            projects_map if settings.available else None,
        )
        result.append(
            RCProject(
                name=_basename(path),
                directory=path,
                trusted=decision is TrustDecision.TRUSTED,
                in_list=path in enabled,
                status=status,
                auto_start=path in enabled,
                rc_at_startup_setting=read_rc_at_startup(path),
                spawn_mode=str(spawn) if spawn else None,
                dir_exists=dir_exists,
                trust_decision=decision,
            )
        )
    return RCScanResult(
        result,
        settings,
        rc_outcomes.window_inventory_issues(inventory),
        enabled_result,
    )


order_by_activity = rc_outcomes.order_by_activity


def scan_servers_result(
    *,
    window_inventory: tmux.WindowInventory | None = None,
    proc_inventory: proc.ProcRCInventory | None = None,
    environment_cache: rc_environment.EnvironmentIdCache | None = None,
) -> RCServerScanResult:
    """All project RC servers: managed (csctl tmux) ∪ external (/proc) — R5/D5.

    Managed = tmux windows in `cfg.rc_session` (their pane pid IS the server
    pid); external = `/proc`-discovered `claude remote-control --name` processes
    NOT owned by a managed pane. External servers are READ-ONLY (no
    takeover/restart — review gate; sustains the "no auto-restart RC" rule).

    For managed servers the captured `env_*` cloud id is returned on `RCServer`;
    this scan is read-only. The caller passes those observations to environment
    reconciliation, the sole ledger writer. The lower tmux and proc adapters own
    expected external failures; parser and programming failures stay observable.
    """
    window_scan = (
        _tmux_window_inventory() if window_inventory is None else window_inventory
    )
    process_scan = (
        proc.scan_rc_server_inventory() if proc_inventory is None else proc_inventory
    )
    windows = window_scan.records
    discovered = process_scan.records
    cache = _environment_ids if environment_cache is None else environment_cache
    environment_resolution = cache.resolve_result(
        windows,
        _tmux_capture_pane_result,
    )
    captured_env_ids = environment_resolution.environment_ids

    by_pid = {p.pid: p for p in discovered}
    managed_pid_set = {w.pid for w in windows if w.pid}

    servers: list[RCServer] = []
    # Managed windows first — tmux is the authority for "managed". Addressed
    # by the server-unique window id, never by the (collision-prone) name.
    for w in windows:
        status: Status = "dead" if w.dead else "running"
        found = by_pid.get(w.pid) if w.pid else None
        env_id = captured_env_ids.get(w.wid, "")
        servers.append(
            RCServer(
                name=found.name if found else w.name,
                cwd=found.cwd if found else w.path,
                managed=True,
                pid=w.pid or None,
                env_id=env_id or None,
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
                env_id=None,
                status="running",
            )
        )

    issues = (
        *rc_outcomes.window_inventory_issues(window_scan),
        *rc_outcomes.proc_inventory_issues(process_scan),
        *rc_outcomes.environment_capture_issues(environment_resolution),
    )
    return RCServerScanResult(tuple(servers), issues)


def scan_servers(
    *,
    environment_cache: rc_environment.EnvironmentIdCache | None = None,
) -> list[RCServer]:
    """Compatibility records-only view of :func:`scan_servers_result`."""

    return list(scan_servers_result(environment_cache=environment_cache).servers)


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
    issues = rc_outcomes.window_inventory_issues(inventory)
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
        cfg.rc_session, tmux.session_name_for(path), cmd
    )
    if not create_result.success:
        return rc_outcomes.start_from_tmux(StartState.TMUX_FAILED, path, create_result)
    target = create_result.target
    if target is None:
        raise AssertionError("successful tmux create must carry a target")
    # Declare the window's project — the collision-safe join key `scan` and
    # `stop_one` read back. Until this lands, `pane_current_path` (the `cd`
    # above) covers the same join, so a mid-spawn scan still matches.
    metadata_result = tmux.set_window_option_result(target, "@csctl_path", path)
    _environment_ids.invalidate_all()
    state = (
        StartState.STARTED if metadata_result.success else StartState.METADATA_FAILED
    )
    return rc_outcomes.start_from_tmux(state, path, metadata_result)


def start_one_result(path: str) -> StartResult:
    """Start one RC server with tri-state trust evidence."""

    return _start_one_with_trust(path, trust_decision(path))


def start_one(path: str) -> bool:
    """Compatibility bool; unavailable trust still fails closed."""

    return start_one_result(path).success


def stop_one_result(
    path: str,
    *,
    window_inventory: tmux.WindowInventory | None = None,
) -> StopResult:
    inventory = (
        _tmux_window_inventory() if window_inventory is None else window_inventory
    )
    issues = rc_outcomes.window_inventory_issues(inventory)
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
        _environment_ids.invalidate_window(win.wid)
        return StopResult(StopState.STOPPED, path)
    if kill_result.state is tmux.KillState.TARGET_NOT_FOUND:
        return StopResult(StopState.NOT_RUNNING, path, kill_result.detail)
    return StopResult(StopState.FAILED, path, kill_result.detail)


def stop_one(path: str) -> bool:
    """Compatibility bool view of ``stop_one_result``."""

    return stop_one_result(path).success


def remove_one_result(path: str) -> RemoveResult:
    """Remove from autostart and retain whether stopping the window failed."""

    enabled_list = list_rm_result(path)
    if not enabled_list.success:
        return RemoveResult(enabled_list, None)
    return RemoveResult(enabled_list, stop_one_result(path))


def stop_all_result() -> StopAllResult:
    """Stop the configured RC tmux session without conflating absence/failure."""

    kill_result = tmux.kill_session_result(cfg.rc_session)
    if kill_result.state is tmux.KillState.KILLED:
        _environment_ids.invalidate_all()
        return StopAllResult(StopState.STOPPED, cfg.rc_session)
    if kill_result.state is tmux.KillState.TARGET_NOT_FOUND:
        return StopAllResult(
            StopState.NOT_RUNNING,
            cfg.rc_session,
            kill_result.detail,
        )
    return StopAllResult(StopState.FAILED, cfg.rc_session, kill_result.detail)


def start_many_result(projects: list[str]) -> StartManyResult:
    """Start a batch while retaining trust-unavailable refusals."""

    results: list[StartResult] = []
    any_target_created = False
    for project in projects:
        if any_target_created:
            time.sleep(cfg.rc_stagger)
        result = start_one_result(project)
        results.append(result)
        any_target_created = any_target_created or result.target is not None
    return rc_outcomes.summarize_starts(results)


def start_all_listed_result() -> StartManyResult:
    """Typed batch result for operator-facing callers."""

    enabled_list = list_enabled_result()
    if not enabled_list.success:
        return StartManyResult(enabled_list=enabled_list)
    if enabled_list.value is None:
        raise AssertionError("successful enabled-list result must carry paths")
    return replace(
        start_many_result(list(enabled_list.value)),
        enabled_list=enabled_list,
    )
