"""Bundled agent-skill management (`csctl skill`).

The package ships its own Claude Code skill (SKILL.md) so the session-doctor
knowledge travels with the CLI. Install is explicit — never a postinstall side
effect: the user runs `csctl skill install` and gets one skill directory under
`~/.claude/skills/<SKILL_NAME>/` containing SKILL.md only (all executable
capability lives in csctl subcommands, not in copied scripts).
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

from ..config import cfg

SKILL_NAME = "claude-session-doctor"


def bundled_skill_text() -> str:
    return (
        resources.files("cc_session_control")
        .joinpath("skill/SKILL.md")
        .read_text(encoding="utf-8")
    )


def skill_target_dir() -> Path:
    return cfg.skills_dir / SKILL_NAME


def install(force: bool = False) -> tuple[bool, str]:
    """Write the bundled skill. Returns (ok, message).

    An existing skill directory is only replaced with `force` — it may hold a
    hand-maintained variant (e.g. the pre-csctl script-based skill), and
    silently clobbering it would hide that migration.
    """
    target = skill_target_dir()
    if target.exists():
        if not force:
            return False, (
                f"Refused: {target} already exists. "
                "Re-run with --force to replace it (the old directory is removed)."
            )
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(bundled_skill_text(), encoding="utf-8")
    return True, f"Installed skill '{SKILL_NAME}' -> {target}"


def uninstall() -> tuple[bool, str]:
    target = skill_target_dir()
    if not target.exists():
        return False, f"Not installed: {target}"
    shutil.rmtree(target)
    return True, f"Removed {target}"
