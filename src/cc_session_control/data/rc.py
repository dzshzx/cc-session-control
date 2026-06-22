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


def trusted_projects() -> list[str]:
    ws = str(cfg.workspace)
    try:
        with open(cfg.claude_json) as f:
            data = json.load(f)
        prefix = ws + "/"
        projects = []
        for key, val in data.get("projects", {}).items():
            if val.get("hasTrustDialogAccepted") and key.startswith(prefix):
                name = key[len(prefix):]
                if "/" not in name:
                    projects.append(name)
        return sorted(projects)
    except Exception:
        return []


def is_trusted(proj: str) -> bool:
    try:
        with open(cfg.claude_json) as f:
            data = json.load(f)
        key = f"{cfg.workspace}/{proj}"
        return data.get("projects", {}).get(key, {}).get("hasTrustDialogAccepted", False)
    except Exception:
        return False


def _tmux_windows() -> list[str]:
    try:
        out = subprocess.run(
            ["tmux", "list-windows", "-t", cfg.rc_session, "-F", "#W"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _is_alive(proj: str) -> bool:
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-t", f"{cfg.rc_session}:{proj}", "-F", "#{pane_dead}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return out.strip().split("\n")[0] == "0"
    except Exception:
        return False


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
    try:
        has_session = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, timeout=5,
        ).returncode == 0
    except Exception:
        has_session = False

    try:
        if has_session:
            subprocess.run(["tmux", "new-window", "-t", session, "-n", proj, cmd],
                           capture_output=True, timeout=5)
        else:
            subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", proj, cmd],
                           capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def stop_one(proj: str) -> bool:
    try:
        return subprocess.run(
            ["tmux", "kill-window", "-t", f"{cfg.rc_session}:{proj}"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except Exception:
        return False


def stop_all() -> bool:
    try:
        return subprocess.run(
            ["tmux", "kill-session", "-t", cfg.rc_session],
            capture_output=True, timeout=5,
        ).returncode == 0
    except Exception:
        return False


def start_many(projects: list[str]) -> int:
    count = 0
    for proj in projects:
        if count > 0:
            time.sleep(cfg.rc_stagger)
        if start_one(proj):
            count += 1
    return count
