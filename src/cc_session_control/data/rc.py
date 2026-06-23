"""RC workspace management — manage Claude Code Remote Control via tmux."""

from __future__ import annotations

import json
import os
import subprocess
import time

from ..config import cfg
from ..models import RCProject


def _ensure_list() -> None:
    os.makedirs(cfg.config_dir, exist_ok=True)
    if not cfg.rc_list.is_file():
        cfg.rc_list.touch()


def list_enabled() -> list[str]:
    _ensure_list()
    try:
        return [
            line.strip() for line in cfg.rc_list.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except FileNotFoundError:
        return []


def list_has(proj: str) -> bool:
    return proj in list_enabled()


def list_add(proj: str) -> None:
    _ensure_list()
    if list_has(proj):
        return
    with open(cfg.rc_list, "a") as f:
        f.write(f"{proj}\n")


def list_rm(proj: str) -> None:
    _ensure_list()
    try:
        lines = cfg.rc_list.read_text().splitlines(keepends=True)
        with open(cfg.rc_list, "w") as f:
            for line in lines:
                if line.strip() != proj:
                    f.write(line)
    except FileNotFoundError:
        pass


def toggle_autostart(proj: str) -> bool:
    """Toggle project in the autostart list. Returns new state."""
    if list_has(proj):
        list_rm(proj)
        return False
    list_add(proj)
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


def trusted_projects() -> list[str]:
    prefix = str(cfg.workspace) + "/"
    projects = []
    try:
        for key, val in _load_projects().items():
            if val.get("hasTrustDialogAccepted") and key.startswith(prefix):
                name = key[len(prefix):]
                if "/" not in name:
                    projects.append(name)
    except Exception:
        return []
    return sorted(projects)


def is_trusted(proj: str) -> bool:
    try:
        key = f"{cfg.workspace}/{proj}"
        return bool(_load_projects().get(key, {}).get("hasTrustDialogAccepted", False))
    except Exception:
        return False


# --- tmux adapter ---------------------------------------------------------
# Single seam over the tmux CLI. Only `_tmux_run` touches `subprocess`; every
# other tmux call routes through a verb wrapper. Each wrapper keeps the
# swallow-errors contract (return empty/False/None on any failure).


def _tmux_run(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run one `tmux <args>` command; return the result, or None on failure."""
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None


def _tmux_list_windows() -> list[str]:
    cp = _tmux_run(["list-windows", "-t", cfg.rc_session, "-F", "#W"])
    if cp is None:
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def _tmux_pane_alive(target: str) -> bool:
    cp = _tmux_run(["list-panes", "-t", target, "-F", "#{pane_dead}"])
    if cp is None:
        return False
    return cp.stdout.strip().split("\n")[0] == "0"


def _tmux_has_session(session: str) -> bool:
    cp = _tmux_run(["has-session", "-t", session])
    return cp is not None and cp.returncode == 0


def _tmux_new_window(session: str, name: str, cmd: str) -> bool:
    return _tmux_run(["new-window", "-t", session, "-n", name, cmd]) is not None


def _tmux_new_session(session: str, name: str, cmd: str) -> bool:
    return _tmux_run(["new-session", "-d", "-s", session, "-n", name, cmd]) is not None


def _tmux_kill_window(target: str) -> bool:
    cp = _tmux_run(["kill-window", "-t", target])
    return cp is not None and cp.returncode == 0


def _tmux_kill_session(session: str) -> bool:
    cp = _tmux_run(["kill-session", "-t", session])
    return cp is not None and cp.returncode == 0


def _tmux_windows() -> list[str]:
    return _tmux_list_windows()


def _is_alive(proj: str) -> bool:
    return _tmux_pane_alive(f"{cfg.rc_session}:{proj}")


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
    enabled = set(list_enabled())
    trusted = trusted_projects()
    windows = set(_tmux_windows())
    all_names = sorted(set(trusted) | enabled)

    result: list[RCProject] = []
    for name in all_names:
        directory = str(cfg.workspace / name)
        in_windows = name in windows
        if in_windows:
            status = "running" if _is_alive(name) else "dead"
        else:
            status = "stopped"
        result.append(RCProject(
            name=name, directory=directory,
            trusted=name in trusted,
            in_list=name in enabled,
            status=status,
            auto_start=name in enabled,
            rc_at_startup=_read_rc_at_startup(directory),
        ))
    return result


def start_one(proj: str) -> bool:
    directory = cfg.workspace / proj
    if not directory.is_dir():
        return False
    if not is_trusted(proj):
        return False
    if proj in _tmux_windows():
        return False

    cmd = (
        f"cd {directory} && delay=5; while true; do start=$(date +%s); "
        f"claude remote-control --name ws/{proj} --spawn same-dir; "
        f"elapsed=$(( $(date +%s) - start )); "
        f"if [ $elapsed -ge 120 ]; then delay=5; "
        f"elif [ $delay -lt 60 ]; then delay=$(( delay * 2 )); fi; "
        f"[ $delay -gt 60 ] && delay=60; "
        f'echo "[csctl] ws/{proj} exited, restart in ${{delay}}s..."; '
        f"sleep $delay; done"
    )

    session = cfg.rc_session
    has_session = _tmux_has_session(session)

    if has_session:
        return _tmux_new_window(session, proj, cmd)
    return _tmux_new_session(session, proj, cmd)


def stop_one(proj: str) -> bool:
    return _tmux_kill_window(f"{cfg.rc_session}:{proj}")


def stop_all() -> bool:
    return _tmux_kill_session(cfg.rc_session)


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
