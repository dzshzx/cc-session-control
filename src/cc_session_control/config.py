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
