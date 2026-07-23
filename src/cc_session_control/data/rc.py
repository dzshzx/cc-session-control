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
import re
import shlex
import time

from ..config import cfg
from ..models import (
    EnvRecord,
    RCProject,
    RCServer,
    Session,
    effective_trust,
    split_env_id,
)
from . import environments, proc, tmux

# Cloud bridge env id printed to a managed server's pane (`environment=env_…`).
_ENV_ID_RE = re.compile(r"env_[A-Za-z0-9]+")


def _ensure_list() -> None:
    os.makedirs(cfg.config_dir, exist_ok=True)
    if not cfg.rc_list.is_file():
        cfg.rc_list.touch()


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
        dirs = [k for k in _load_projects() if "/" in k]
        if dirs:
            common = os.path.commonpath(dirs)
            if os.path.isdir(common) and common != os.path.expanduser("~"):
                return common
    except Exception:
        pass
    return os.getcwd()


def _migrate_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Absolute-path-ify legacy rc-enabled lines.

    Comments/blank lines pass through verbatim; absolute paths are kept;
    anything else (pre-0.7.3 short names, including relative `a/b` forms)
    resolves against the frozen legacy workspace root. Idempotent — a fully
    migrated file reports changed=False, so no rewrite is triggered.
    """
    out: list[str] = []
    changed = False
    root: str | None = None
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("/"):
            out.append(raw)
            continue
        if root is None:
            root = _legacy_workspace_root()
        out.append(os.path.join(root, s))
        changed = True
    return out, changed


def _write_list(lines: list[str]) -> None:
    """Atomic rc-enabled rewrite (unique tmp + rename) — a concurrent reader
    never sees a truncated file and concurrent writers cannot interleave."""
    _ensure_list()
    tmp = cfg.rc_list.parent / f".{cfg.rc_list.name}.{os.getpid()}.tmp"
    tmp.write_text("".join(f"{line}\n" for line in lines))
    os.replace(tmp, cfg.rc_list)


def list_enabled() -> list[str]:
    _ensure_list()
    try:
        raw = cfg.rc_list.read_text().splitlines()
    except FileNotFoundError:
        return []
    migrated, changed = _migrate_lines(raw)
    if changed:
        _write_list(migrated)
        raw = migrated
    return [
        line.strip() for line in raw
        if line.strip() and not line.strip().startswith("#")
    ]


def list_has(path: str) -> bool:
    return path in list_enabled()


def list_add(path: str) -> None:
    _ensure_list()
    if list_has(path):
        return
    with open(cfg.rc_list, "a") as f:
        f.write(f"{path}\n")


def list_rm(path: str) -> None:
    try:
        lines = cfg.rc_list.read_text().splitlines()
    except FileNotFoundError:
        return
    _write_list([line for line in lines if line.strip() != path])


def toggle_autostart(path: str) -> bool:
    """Toggle project in the autostart list. Returns new state."""
    if list_has(path):
        list_rm(path)
        return False
    list_add(path)
    return True


def _load_projects() -> dict:
    """Read the `projects` map from ~/.claude.json, or {} on any failure.

    Single source for the claude.json read shared by trusted_projects /
    is_trusted, so the open+parse+swallow dance lives in one place.
    """
    try:
        with open(cfg.claude_json) as f:
            return json.load(f).get("projects", {}) or {}
    except Exception:
        return {}


def _trusted_in(projects: dict) -> set[str]:
    """Effectively-trusted absolute-path keys of a claude.json projects map."""
    return {
        key for key in projects
        if isinstance(key, str) and key.startswith("/")
        and effective_trust(key, projects)
    }


def trusted_projects() -> list[str]:
    """Absolute paths of every effectively-trusted claude.json project entry.

    Membership base of the 项目 tab. Directory existence and residue handling
    stay in `scan()` (unchanged split)."""
    return sorted(_trusted_in(_load_projects()))


def is_trusted(path: str) -> bool:
    try:
        return effective_trust(path, _load_projects())
    except Exception:
        return False


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
                val = json.load(f).get("remoteControlAtStartup")
            if val is not None:
                return bool(val)
        except Exception:
            continue
    return None


def set_rc_at_startup(directory: str, value: bool | None) -> None:
    settings_dir = os.path.join(directory, ".claude")
    path = os.path.join(settings_dir, "settings.local.json")
    os.makedirs(settings_dir, exist_ok=True)
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {}
    if value is None:
        data.pop("remoteControlAtStartup", None)
    else:
        data["remoteControlAtStartup"] = value
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def scan() -> list[RCProject]:
    # ONE claude.json load feeds membership, trust flags and spawn modes —
    # no per-project re-parse.
    projects_map = _load_projects()
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
        if win is not None:
            status = "dead" if win.dead else "running"
        else:
            status = "stopped"
        entry = projects_map.get(path)
        spawn = entry.get("remoteControlSpawnMode") if isinstance(entry, dict) else None
        result.append(RCProject(
            name=_basename(path), directory=path,
            trusted=path in trusted,
            in_list=path in enabled,
            status=status,
            auto_start=path in enabled,
            rc_at_startup=_read_rc_at_startup(path),
            spawn_mode=str(spawn) if spawn else None,
            dir_exists=dir_exists,
        ))
    return result


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
    m = _ENV_ID_RE.search(_tmux_capture_pane(target))
    return m.group(0) if m else ""


def scan_servers() -> list[RCServer]:
    """All project RC servers: managed (csctl tmux) ∪ external (/proc) — R5/D5.

    Managed = tmux windows in `cfg.rc_session` (their pane pid IS the server
    pid); external = `/proc`-discovered `claude remote-control --name` processes
    NOT owned by a managed pane. External servers are READ-ONLY (no
    takeover/restart — review gate; sustains the "no auto-restart RC" rule).

    For managed servers the captured `env_*` cloud id is pushed one-way into the
    ledger via `environments.upsert` (rc → environments only; environments never
    imports rc). Swallows errors → returns whatever it assembled.
    """
    try:
        windows = _tmux_windows()
        discovered = proc.scan_rc_servers()
    except Exception:
        return []

    by_pid = {p.pid: p for p in discovered}
    managed_pid_set = {w.pid for w in windows if w.pid}

    servers: list[RCServer] = []
    env_records: list[EnvRecord] = []

    # Managed windows first — tmux is the authority for "managed". Addressed
    # by the server-unique window id, never by the (collision-prone) name.
    for w in windows:
        status = "dead" if w.dead else "running"
        found = by_pid.get(w.pid) if w.pid else None
        env_id = _capture_env_id(w.wid)
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


def start_one(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    if not is_trusted(path):
        return False
    win = _window_for(path)
    if win is not None:
        if not win.dead:
            return False
        if not stop_one(path):
            return False

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
        return False
    # Declare the window's project — the collision-safe join key `scan` and
    # `stop_one` read back. Until this lands, `pane_current_path` (the `cd`
    # above) covers the same join, so a mid-spawn scan still matches.
    tmux.set_window_option(target, "@csctl_path", path)
    return True


def stop_one(path: str) -> bool:
    win = _window_for(path)
    if win is None:
        return False
    return tmux.kill_window(win.wid)


def stop_all() -> bool:
    return tmux.kill_session(cfg.rc_session)


def start_many(projects: list[str]) -> int:
    count = 0
    for proj in projects:
        if count > 0:
            time.sleep(cfg.rc_stagger)
        if start_one(proj):
            count += 1
    return count


def start_all_listed() -> int:
    """Start every project currently enabled in the autostart list."""
    return start_many(list_enabled())
