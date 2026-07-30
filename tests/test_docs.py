"""Documentation contracts for public CLI examples and architecture seams."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from cc_session_control.cli import build_parser

README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
CLAUDE = (Path(__file__).parents[1] / "CLAUDE.md").read_text(encoding="utf-8")


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


@pytest.mark.parametrize(
    "settled_term",
    [
        "`transcripts.py`",
        "`sessions.scan_result(inputs)`",
        "`proc.scan_rc_server_inventory()`",
        "`resolve_execution_session`",
        "`tmux.residency_inventory`",
        "`Session.tmux_inventory_complete`",
        "`Session.tmux_inventory_detail`",
        "ASCII `?`",
        "`_tmux_run_result`",
        "`capture_pane_result`",
        "`tmux.run_in_tmux_result`",
        "`tmux_outcomes.py`",
        "`LedgerUpdate`",
        "`_tmux_window_inventory`",
        "`_tmux_capture_pane_result`",
        "`_window_for_inventory`",
        "`EnabledListResult`",
        "`operation` / `stage` / `detail` / `changed` / `committed`",
        "`take_over_result`",
        "`stop_job_result`",
        "`prepare_takeover`",
        "`respawn_result`",
        "`proc.probe_pid`",
        "`proc.probe_current_ancestors().complete`",
        "`atomic_write.py`",
    ],
)
def test_claude_architecture_uses_settled_typed_seams(settled_term: str) -> None:
    assert settled_term in CLAUDE


@pytest.mark.parametrize(
    "stale_claim",
    [
        "`sessions.scan()`",
        "`sessions.scan(inputs)`",
        "`proc.scan_rc_servers()`",
        "`tmux.residency_targets`",
        "`tmux.run_in_tmux`",
        "`environments.upsert`",
        "`_tmux_windows`",
        "`_window_for`",
        "只有它的 `_tmux_run` 触碰 `subprocess`",
        "ledger 是 **CLI-only**",
        "`terminate_session`",
        "`stop_job`",
        "`resume_takeover`",
        "`respawn`",
        "`current_determinable`",
        "`proc.pid_exists`",
        "`list_enabled`",
        "`toggle_autostart` primitive",
        "`capture_pane`",
        "`residency_targets`",
        "`list_orphan_dirs`",
        "`pid_alive`",
        "`start_one`",
        "`job_host`",
        "`EnvRow`",
    ],
)
def test_claude_architecture_rejects_retired_seam_claims(
    stale_claim: str,
) -> None:
    assert stale_claim not in CLAUDE
