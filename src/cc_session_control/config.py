"""Path detection and configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path


class Config:
    def __init__(self) -> None:
        self.claude_home: Path = Path.home() / ".claude"
        self.claude_json: Path = Path.home() / ".claude.json"
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        self.config_dir: Path = Path(xdg) / "csctl"
        self.rc_list: Path = self.config_dir / "rc-enabled"
        self.rc_session: str = os.environ.get("CSCTL_RC_SESSION", "rc")
        self.rc_stagger: int = int(os.environ.get("CSCTL_RC_STAGGER", "2"))
        # Dedicated tmux session for interactive sessions relaunched under remote
        # control (kept separate from rc_session, whose windows are managed RC
        # server processes).
        self.tmux_session: str = os.environ.get("CSCTL_TMUX_SESSION", "cc")
        # Age threshold (days) for the time/global-keyed cleanup strategy.
        self.cleanup_age_days: int = int(os.environ.get("CSCTL_CLEANUP_AGE_DAYS", "14"))
        self._workspace: Path | None = None

    @property
    def workspace(self) -> Path:
        if self._workspace is not None:
            return self._workspace
        self._workspace = _detect_workspace(self.claude_json)
        return self._workspace

    @workspace.setter
    def workspace(self, value: Path) -> None:
        self._workspace = value

    @property
    def projects_root(self) -> Path:
        return self.claude_home / "projects"

    # --- Claude Code state directories (single path authority) ---
    # All derive from claude_home so tests that monkeypatch cfg.claude_home flow
    # through. Never inline `claude_home / "..."` elsewhere — add it here.

    @property
    def sessions_dir(self) -> Path:
        """Per-pid session registry files (`sessions/<pid>.json`)."""
        return self.claude_home / "sessions"

    @property
    def jobs_dir(self) -> Path:
        """Background agent job state (`jobs/<short>/state.json`)."""
        return self.claude_home / "jobs"

    @property
    def session_env_dir(self) -> Path:
        """Per-session env artifacts (`session-env/<sid>`)."""
        return self.claude_home / "session-env"

    @property
    def file_history_dir(self) -> Path:
        """Per-session file-edit history (`file-history/<sid>`)."""
        return self.claude_home / "file-history"

    @property
    def shell_snapshots_dir(self) -> Path:
        return self.claude_home / "shell-snapshots"

    @property
    def telemetry_dir(self) -> Path:
        return self.claude_home / "telemetry"

    @property
    def plans_dir(self) -> Path:
        return self.claude_home / "plans"

    @property
    def backups_dir(self) -> Path:
        return self.claude_home / "backups"

    @property
    def paste_cache_dir(self) -> Path:
        return self.claude_home / "paste-cache"

    @property
    def debug_dir(self) -> Path:
        return self.claude_home / "debug"

    @property
    def uploads_dir(self) -> Path:
        return self.claude_home / "uploads"

    @property
    def tasks_dir(self) -> Path:
        return self.claude_home / "tasks"


def _detect_workspace(claude_json: Path) -> Path:
    env = os.environ.get("CSCTL_WORKSPACE")
    if env:
        return Path(env)

    default = Path.home() / "workspace"
    if default.is_dir():
        return default

    try:
        with open(claude_json) as f:
            data = json.load(f)
        dirs = [k for k in data.get("projects", {}) if "/" in k]
        if dirs:
            from os.path import commonpath
            common = Path(commonpath(dirs))
            if common.is_dir() and common != Path.home():
                return common
    except Exception:
        pass

    return Path.cwd()


cfg = Config()
