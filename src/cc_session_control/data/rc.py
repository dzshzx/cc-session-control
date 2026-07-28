"""RC project management — manage Claude Code Remote Control via tmux.

Projects are keyed by ABSOLUTE DIRECTORY PATH (no workspace-root concept):
membership = a `~/.claude.json` projects entry that is effectively trusted
(`models.effective_trust` — claude's own ancestor-inheriting dialog gate).
`RCProject.name` is a derived basename for display only; every join
(rc-enabled list, tmux windows via `@csctl_path`, claude.json lookups) uses
the path.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
from dataclasses import dataclass
from enum import Enum

from ..config import cfg
from ..models import (
    EnvRecord,
    RCProject,
    RCServer,
    Session,
    TrustDecision,
    effective_trust_decision,
    split_env_id,
)
from . import environments, proc, rc_environment, tmux
from .project_settings import (
    ProjectSettingsResult,
    SettingWriteResult,
    read_project_settings,
    write_rc_at_startup,
)
from .rc_enabled import EnabledListStore, migrate_lines

_environment_ids = rc_environment.EnvironmentIdCache()


@dataclass(frozen=True)
class RCScanResult:
    """Project rows plus the settings evidence used to derive trust."""

    projects: list[RCProject]
    settings: ProjectSettingsResult


@dataclass(frozen=True)
class ProjectTrustResult:
    """One trust decision and the settings evidence behind it."""

    decision: TrustDecision
    settings: ProjectSettingsResult


class StartState(Enum):
    """Observable outcome of starting one project RC server."""

    STARTED = "started"
    NOT_DIRECTORY = "not-directory"
    TRUST_UNAVAILABLE = "trust-unavailable"
    UNTRUSTED = "untrusted"
    ALREADY_RUNNING = "already-running"
    STOP_FAILED = "stop-failed"
    TMUX_FAILED = "tmux-failed"


@dataclass(frozen=True)
class StartResult:
    state: StartState
    path: str

    @property
    def success(self) -> bool:
        return self.state is StartState.STARTED


@dataclass(frozen=True)
class StartManyResult:
    started: int = 0
    unavailable: int = 0
    untrusted: int = 0
    failed: int = 0


def _legacy_workspace_root() -> str:
    """FROZEN pre-0.7.3 workspace detection — rc-enabled migration ONLY.

    Replicates the deleted `config._detect_workspace` order (CSCTL_WORKSPACE
    override → `~/workspace` → commonpath of claude.json keys → cwd) so legacy
    short-name lines resolve exactly as the old csctl resolved them. Dead by
    design once a machine's list has been rewritten; never reuse elsewhere.
    """
    env = os.environ.get("CSCTL_WORKSPACE")
    if env:
        return env
    default = os.path.join(os.path.expanduser("~"), "workspace")
    if os.path.isdir(default):
        return default
    try:
        dirs = [
            key for key in _load_projects()
            if isinstance(key, str) and "/" in key
        ]
        if dirs:
            common = os.path.commonpath(dirs)
            if os.path.isdir(common) and common != os.path.expanduser("~"):
                return common
    except (OSError, ValueError):
        pass
    return os.getcwd()


def _migrate_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Compatibility wrapper for legacy migration tests."""

    return migrate_lines(lines, _legacy_workspace_root)


def _enabled_store() -> EnabledListStore:
    return EnabledListStore(cfg.rc_list, _legacy_workspace_root)

def list_enabled() -> list[str]:
    return _enabled_store().list()


def list_has(path: str) -> bool:
    return _enabled_store().contains(path)


def list_add(path: str) -> None:
    _enabled_store().add(path)


def list_rm(path: str) -> None:
    _enabled_store().remove(path)


def toggle_autostart(path: str) -> bool:
    """Toggle project in the autostart list. Returns new state."""
    return _enabled_store().toggle(path)


def _load_projects() -> dict:
    """Compatibility map-only reader; typed callers use ``_read_projects``.

    Failures become an empty map only at this legacy boundary. Safety decisions
    never call it, so they retain ``UNAVAILABLE``.
    """
    return _read_projects().projects


def _read_projects() -> ProjectSettingsResult:
    """Typed single source for ``~/.claude.json`` project metadata."""

    return read_project_settings(cfg.claude_json)


def _trusted_in(projects: dict) -> set[str]:
    """Effectively-trusted absolute-path keys of a claude.json projects map."""
    return {
        key for key in projects
        if isinstance(key, str) and key.startswith("/")
        and effective_trust_decision(key, projects) is TrustDecision.TRUSTED
    }


# Platform temp roots — working space, never projects. Keeping a temp root
# trusted (so throwaway sessions skip the dialog) must not surface it in the
# launcher; this is a MEMBERSHIP rule, the trust state itself is untouched.
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


def trusted_projects() -> list[str]:
    """Absolute paths of every effectively-trusted claude.json project entry.

    Membership base of the 项目 tab. Directory existence and residue handling
    stay in `scan()` (unchanged split)."""
    return sorted(_trusted_in(_load_projects()))


def project_trust(path: str) -> ProjectTrustResult:
    """Effective trust plus typed settings evidence."""

    settings = _read_projects()
    projects = settings.projects if settings.available else None
    return ProjectTrustResult(effective_trust_decision(path, projects), settings)


def trust_decision(path: str) -> TrustDecision:
    """Compatibility decision-only view of ``project_trust``."""

    return project_trust(path).decision


def is_trusted(path: str) -> bool:
    """Compatibility bool; unavailable evidence fails closed."""

    return trust_decision(path) is TrustDecision.TRUSTED


def _basename(path: str) -> str:
    """Display name derived from the path — NEVER an identity key."""
    return os.path.basename(path.rstrip("/")) or path


# --- RC-scoped thin delegates over data/tmux.py ---------------------------
# Bound to `cfg.rc_session`. The generic tmux adapter lives in `data/tmux.py`
# (the single seam — only its `_tmux_run` touches `subprocess`); these stay
# here (rather than inlined at call sites) because `scan`/`scan_servers` and
# their tests poke these exact RC-scoped names.


def _tmux_windows() -> list[tmux.TmuxWindow]:
    return tmux.list_windows_meta(cfg.rc_session)


def _tmux_capture_pane(target: str) -> str:
    return tmux.capture_pane(target)


def _window_for(path: str) -> tmux.TmuxWindow | None:
    """The managed window belonging to `path`, or None (normpath equality on
    the window's declared/adopted `path` metadata — never by window name)."""
    norm = os.path.normpath(path)
    for w in _tmux_windows():
        if w.path and os.path.normpath(w.path) == norm:
            return w
    return None


def _read_rc_at_startup(directory: str) -> bool | None:
    for name in ("settings.local.json", "settings.json"):
        path = os.path.join(directory, ".claude", name)
        try:
            with open(path) as f:
                document = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(document, dict):
            continue
        value = document.get("remoteControlAtStartup")
        if isinstance(value, bool):
            return value
    return None


def set_rc_at_startup(
    directory: str,
    value: bool | None,
) -> SettingWriteResult:
    """Typed compatibility name for the atomic project-settings writer."""

    return write_rc_at_startup(directory, value)


def scan_result() -> RCScanResult:
    # ONE claude.json load feeds membership, trust flags and spawn modes —
    # no per-project re-parse.
    settings = _read_projects()
    projects_map = settings.projects
    trusted = _trusted_in(projects_map)
    enabled = set(list_enabled())
    by_path = {
        os.path.normpath(w.path): w for w in _tmux_windows() if w.path
    }

    result: list[RCProject] = []
    for path in sorted(trusted | enabled):
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
            status = "dead" if win.dead else "running"
        else:
            status = "stopped"
        entry = projects_map.get(path)
        spawn = entry.get("remoteControlSpawnMode") if isinstance(entry, dict) else None
        decision = effective_trust_decision(
            path, projects_map if settings.available else None,
        )
        result.append(RCProject(
            name=_basename(path), directory=path,
            trusted=decision is TrustDecision.TRUSTED,
            in_list=path in enabled,
            status=status,
            auto_start=path in enabled,
            rc_at_startup=_read_rc_at_startup(path),
            spawn_mode=str(spawn) if spawn else None,
            dir_exists=dir_exists,
            trust_decision=decision,
        ))
    return RCScanResult(result, settings)


def scan() -> list[RCProject]:
    """Compatibility list-only scan; new surfaces use ``scan_result``."""

    return scan_result().projects


def order_by_activity(
    projects: list[RCProject], sessions: list[Session]
) -> list[RCProject]:
    """PURE: most-recently-active projects first (exact-cwd session join).

    THE one ordering — the 项目 tab and `csctl rc status` both call it, so the
    two surfaces can't diverge. A session counts toward the project whose
    directory equals its cwd (normpath); no ancestor roll-up — a subdirectory
    where claude ran is a member itself. Never-active projects sink,
    path-ascending, so broad-root members (a trusted `/tmp`) stay out of the
    way instead of crowding the launcher's top.
    """
    latest: dict[str, float] = {}
    for s in sessions:
        if not s.cwd:
            continue
        key = os.path.normpath(s.cwd)
        if s.mtime > latest.get(key, 0.0):
            latest[key] = s.mtime
    return sorted(
        projects,
        key=lambda p: (-latest.get(os.path.normpath(p.directory), 0.0),
                       p.directory),
    )


def _capture_env_id(target: str) -> str:
    """Grep an `env_*` cloud id from a managed server's pane output, or "".

    The project RC server leaves zero structured footprint; its cloud env id is
    only printed to stdout (`environment=env_…`). This is the single signal we
    can capture locally for the ledger.
    """
    return rc_environment.extract_env_id(_tmux_capture_pane(target))


def scan_servers(
    *,
    environment_cache: rc_environment.EnvironmentIdCache | None = None,
) -> list[RCServer]:
    """All project RC servers: managed (csctl tmux) ∪ external (/proc) — R5/D5.

    Managed = tmux windows in `cfg.rc_session` (their pane pid IS the server
    pid); external = `/proc`-discovered `claude remote-control --name` processes
    NOT owned by a managed pane. External servers are READ-ONLY (no
    takeover/restart — review gate; sustains the "no auto-restart RC" rule).

    For managed servers the captured `env_*` cloud id is pushed one-way into the
    ledger via `environments.upsert` (rc → environments only; environments never
    imports rc). The lower tmux and proc adapters own expected external failures;
    parser and programming failures stay observable.
    """
    windows = _tmux_windows()
    discovered = proc.scan_rc_servers()
    cache = _environment_ids if environment_cache is None else environment_cache
    captured_env_ids = cache.resolve(windows, _capture_env_id)

    by_pid = {p.pid: p for p in discovered}
    managed_pid_set = {w.pid for w in windows if w.pid}

    servers: list[RCServer] = []
    env_records: list[EnvRecord] = []

    # Managed windows first — tmux is the authority for "managed". Addressed
    # by the server-unique window id, never by the (collision-prone) name.
    for w in windows:
        status = "dead" if w.dead else "running"
        found = by_pid.get(w.pid) if w.pid else None
        env_id = captured_env_ids.get(w.wid, "")
        if env_id:
            prefix, key = split_env_id(env_id)
            if prefix and key:
                env_records.append(EnvRecord(prefix=prefix, key=key, bound_sid=None))
        servers.append(RCServer(
            name=found.name if found else w.name,
            cwd=found.cwd if found else w.path,
            managed=True,
            pid=w.pid or None,
            env_id=env_id or None,
            status=status,
        ))

    # External — discovered procs not owned by any managed pane.
    for p in discovered:
        if p.pid in managed_pid_set:
            continue
        servers.append(RCServer(
            name=p.name, cwd=p.cwd, managed=False,
            pid=p.pid or None, env_id=None, status="running",
        ))

    if env_records:
        environments.upsert(env_records)
    return servers


def _start_one_with_trust(path: str, decision: TrustDecision) -> StartResult:
    if not os.path.isdir(path):
        return StartResult(StartState.NOT_DIRECTORY, path)
    if decision is TrustDecision.UNAVAILABLE:
        return StartResult(StartState.TRUST_UNAVAILABLE, path)
    if decision is TrustDecision.UNTRUSTED:
        return StartResult(StartState.UNTRUSTED, path)
    win = _window_for(path)
    if win is not None:
        if not win.dead:
            return StartResult(StartState.ALREADY_RUNNING, path)
        if not stop_one(path):
            return StartResult(StartState.STOP_FAILED, path)

    remote_name = _basename(path)
    # Each fresh Remote Control process registers a distinct cloud environment.
    # Keep restart explicit so transient exits do not pile up duplicate mobile
    # environment entries with the same display name.
    cmd = (
        f"cd {shlex.quote(path)} && exec claude remote-control "
        f"--name {shlex.quote(remote_name)} --spawn same-dir"
    )

    target = tmux.run_in_tmux(cfg.rc_session, tmux.session_name_for(path), cmd)
    if target is None:
        return StartResult(StartState.TMUX_FAILED, path)
    # Declare the window's project — the collision-safe join key `scan` and
    # `stop_one` read back. Until this lands, `pane_current_path` (the `cd`
    # above) covers the same join, so a mid-spawn scan still matches.
    tmux.set_window_option(target, "@csctl_path", path)
    _environment_ids.invalidate_all()
    return StartResult(StartState.STARTED, path)


def start_one_result(path: str) -> StartResult:
    """Start one RC server with tri-state trust evidence."""

    return _start_one_with_trust(path, trust_decision(path))


def start_one(path: str) -> bool:
    """Compatibility bool; unavailable trust still fails closed."""

    return start_one_result(path).success


def stop_one(path: str) -> bool:
    win = _window_for(path)
    if win is None:
        return False
    stopped = tmux.kill_window(win.wid)
    if stopped:
        _environment_ids.invalidate_window(win.wid)
    return stopped


def remove_one(path: str) -> bool:
    """Remove one project from autostart and stop its managed RC window."""
    list_rm(path)
    return stop_one(path)


def stop_all() -> bool:
    stopped = tmux.kill_session(cfg.rc_session)
    if stopped:
        _environment_ids.invalidate_all()
    return stopped


def start_many(projects: list[str]) -> int:
    """Compatibility count-only view of ``start_many_result``."""

    return start_many_result(projects).started


def start_many_result(projects: list[str]) -> StartManyResult:
    """Start a batch while retaining trust-unavailable refusals."""

    started = unavailable = untrusted = failed = 0
    for project in projects:
        if started > 0:
            time.sleep(cfg.rc_stagger)
        result = start_one_result(project)
        if result.state is StartState.STARTED:
            started += 1
        elif result.state is StartState.TRUST_UNAVAILABLE:
            unavailable += 1
        elif result.state is StartState.UNTRUSTED:
            untrusted += 1
        else:
            failed += 1
    return StartManyResult(started, unavailable, untrusted, failed)


def start_all_listed() -> int:
    """Start every project currently enabled in the autostart list."""
    return start_many(list_enabled())


def start_all_listed_result() -> StartManyResult:
    """Typed batch result for operator-facing callers."""

    return start_many_result(list_enabled())
