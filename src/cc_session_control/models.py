"""Data models for cc-session-control."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Session:
    sid: str
    cwd: str
    label: str
    mtime: float
    prompts: int
    pid: int | None
    alive: bool
    current: bool
    hidden: set[str] = field(default_factory=set)
    file: str = ""


@dataclass
class RCProject:
    name: str
    directory: str
    trusted: bool
    in_list: bool
    status: str  # "running" | "dead" | "stopped"
    auto_start: bool
    rc_at_startup: bool | None = None  # per-project remoteControlAtStartup override
    environment_id: str = ""
