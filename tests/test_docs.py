"""Documentation contracts for public CLI examples, architecture seams,
and the CI workflows' external-action pinning policy."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from cc_session_control.cli import build_parser

README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
CLAUDE = (Path(__file__).parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"
QUALITY_GATE_USE = "./.github/workflows/quality-gate.yml"
PINNED_EXTERNAL_USE = re.compile(
    r"^\s*uses:\s*[\w.-]+/[\w.-]+@([0-9a-f]{40})\s+#\s+(\S+)\s*$"
)


@pytest.mark.parametrize(
    "command",
    [
        "resume",
        "resume mybug",
        "resume --page 2",
        "resume --all",
        "agents",
    ],
)
def test_readme_cli_examples_are_accepted_by_the_parser(command: str) -> None:
    assert f"csctl {command}" in README
    build_parser().parse_args(shlex.split(command))


@pytest.mark.parametrize(
    "retired_command", ["csctl prune", "csctl skill", "csctl rc ", "csctl env"]
)
def test_readme_drops_retired_cli_surfaces(retired_command: str) -> None:
    assert retired_command not in README
    assert retired_command not in CLAUDE


@pytest.mark.parametrize(
    "variable",
    [
        "CSCTL_RC_SESSION",
        "CSCTL_CLEANUP_AGE_DAYS",
        "CSCTL_THEME",
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
        "`tmux.run_in_tmux_result`",
        "`tmux_outcomes.py`",
        "`_tmux_window_inventory`",
        "`_window_for_inventory`",
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
        # Autostart-list feature retired in 0.8 — the docs must not resurrect it.
        "`list_enabled`",
        "`toggle_autostart`",
        "`EnabledListResult`",
        "`start_all_listed_result`",
        "rc-enabled",
        "开机自启",
        "`capture_pane`",
        "`residency_targets`",
        "`list_orphan_dirs`",
        "`pid_alive`",
        "`start_one`",
        "`job_host`",
        "`EnvRow`",
        # Bridge-environment ledger pipeline dropped in 0.8 — the docs must
        # not resurrect it.
        "environments.jsonl",
    ],
)
def test_claude_architecture_rejects_retired_seam_claims(
    stale_claim: str,
) -> None:
    assert stale_claim not in CLAUDE


def test_every_external_action_is_pinned_to_a_tagged_commit() -> None:
    # Security policy, not covered by CI merely running: GitHub Actions
    # happily executes an unpinned `uses: owner/repo@main` reference, so
    # a green CI run is no evidence this repo-wide pinning rule holds.
    uses_lines = [
        line
        for workflow in sorted(WORKFLOWS.glob("*.y*ml"))
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("uses:")
    ]

    assert uses_lines
    for line in uses_lines:
        reference = line.split("uses:", 1)[1].strip()
        if reference.startswith("./"):
            assert reference == QUALITY_GATE_USE
        else:
            assert PINNED_EXTERNAL_USE.match(line), line
