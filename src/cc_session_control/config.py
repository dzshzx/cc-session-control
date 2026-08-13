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


_TMUX_TARGET_SYNTAX = frozenset("=:*?[]$@%")


def _literal_tmux_session_environment(name: str, default: str) -> str:
    """Read a tmux session *name*, rejecting target-expression syntax."""

    value = os.environ.get(name, default)
    if not value or any(char in value for char in _TMUX_TARGET_SYNTAX):
        raise ValueError(
            f"{name}={value!r}: expected a literal tmux session name without "
            "= : * ? [ ] $ @ %"
        )
    return value


class Config:
    def __init__(self) -> None:
        self.claude_home: Path = Path.home() / ".claude"
        self.claude_json: Path = Path.home() / ".claude.json"
        # Non-Claude CLI state homes (ADR-0005). Same single-path-authority
        # rule (providers read these, never inline `Path.home() / ".codex"`),
        # honoring each CLI's OFFICIAL relocation variable so csctl scans
        # wherever the CLI itself actually writes.
        self.codex_home: Path = Path(
            os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        )
        self.kimi_home: Path = Path(
            os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code"
        )
        # One operator-facing tmux workspace for every session csctl dispatches.
        # Project identity stays in each window name and cwd; RC servers retain
        # their separate configurable session below.
        self.tmux_session: str = "csctl"
        self.rc_session: str = _literal_tmux_session_environment(
            "CSCTL_RC_SESSION",
            "rc",
        )
        if self.rc_session == self.tmux_session:
            raise ValueError(
                "CSCTL_RC_SESSION must differ from the reserved "
                f"workbench tmux session {self.tmux_session!r}"
            )
        # Age threshold (days) for the time/global-keyed cleanup strategy.
        # 0 is a valid operator value (sweep every aged entry) — pre-0.8
        # releases accepted it, so validation only rejects negatives/garbage.
        self.cleanup_age_days: int = _integer_environment(
            "CSCTL_CLEANUP_AGE_DAYS",
            14,
            minimum=0,
        )
        # TUI palette: "auto" ($COLORFGBG if set, else dark) | "dark" | "light".
        self.theme: str = os.environ.get("CSCTL_THEME", "auto")
        # Allowed agent-CLI providers (ADR-0005). A listed provider is only
        # ACTIVE when its home directory also exists; unknown names are
        # ignored here so the registry stays the single provider authority.
        self.providers: tuple[str, ...] = tuple(
            name.strip()
            for name in os.environ.get(
                "CSCTL_PROVIDERS",
                "claude,codex,kimi",
            ).split(",")
            if name.strip()
        )

    @property
    def projects_root(self) -> Path:
        return self.claude_home / "projects"

    @property
    def config_home(self) -> Path:
        """csctl's own XDG config directory (ADR-0007 curation store)."""
        return (
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "csctl"
        )

    @property
    def curation_file(self) -> Path:
        """Operator curation store (pinned/hidden project directories)."""
        return self.config_home / "projects.json"

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


cfg = Config()
