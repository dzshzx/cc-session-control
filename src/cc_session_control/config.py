"""Path detection and configuration."""

from __future__ import annotations

import os
from pathlib import Path


def _integer_environment(name: str, default: int, minimum: int) -> int:
    """Parse one integer variable with contextual, fail-fast validation."""

    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{name}={raw!r}: expected an integer >= {minimum}",
        ) from None
    if value < minimum:
        raise ValueError(
            f"{name}={raw!r}: expected an integer >= {minimum}",
        )
    return value


class Config:
    def __init__(self) -> None:
        self.claude_home: Path = Path.home() / ".claude"
        self.claude_json: Path = Path.home() / ".claude.json"
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        self.config_dir: Path = Path(xdg) / "csctl"
        self.rc_list: Path = self.config_dir / "rc-enabled"
        self.rc_session: str = os.environ.get("CSCTL_RC_SESSION", "rc")
        self.rc_stagger: int = _integer_environment(
            "CSCTL_RC_STAGGER", 2, minimum=0,
        )
        # Age threshold (days) for the time/global-keyed cleanup strategy.
        self.cleanup_age_days: int = _integer_environment(
            "CSCTL_CLEANUP_AGE_DAYS", 14, minimum=1,
        )
        # TUI palette: "auto" (detect the terminal background) | "dark" | "light".
        self.theme: str = os.environ.get("CSCTL_THEME", "auto")

    @property
    def projects_root(self) -> Path:
        return self.claude_home / "projects"

    @property
    def environments_ledger(self) -> Path:
        """csctl's own append-only bridge-environment ledger (R6).

        Lives under `config_dir` (csctl state, NOT Claude Code's `claude_home`).
        A property so tests that monkeypatch `cfg.config_dir` flow through.
        """
        return self.config_dir / "environments.jsonl"

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

    @property
    def skills_dir(self) -> Path:
        """User-level Claude Code agent skills (`skills/<name>/SKILL.md`)."""
        return self.claude_home / "skills"


cfg = Config()
