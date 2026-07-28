"""Public CLI examples and configuration documented in README.md."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from cc_session_control.cli import build_parser

README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "command",
    [
        "rc status",
        "rc add .",
        "rc add ~/code/app",
        "rc rm ~/code/app",
        "rc up",
        "rc stop all",
        "rc list",
        "prune",
        "prune --max-prompts 1 --apply",
        "prune --sweep-orphans",
        "prune --sweep-zombies",
        "prune --sweep-aged",
        "resume",
        "resume mybug",
        "resume --page 2",
        "resume --all",
        "agents",
        "env",
        "skill install",
        "skill install --force",
        "skill uninstall",
    ],
)
def test_readme_cli_examples_are_accepted_by_the_parser(command: str) -> None:
    assert f"csctl {command}" in README
    build_parser().parse_args(shlex.split(command))


@pytest.mark.parametrize(
    "variable",
    [
        "CSCTL_RC_SESSION",
        "CSCTL_RC_STAGGER",
        "CSCTL_CLEANUP_AGE_DAYS",
        "CSCTL_THEME",
        "XDG_CONFIG_HOME",
    ],
)
def test_readme_lists_every_public_environment_setting(variable: str) -> None:
    assert f"`{variable}`" in README
