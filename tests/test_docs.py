"""Documentation contracts for public CLI examples, architecture seams,
and the CI workflows' external-action pinning policy."""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest

import cc_session_control
from cc_session_control.cli import build_parser

README = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
CLAUDE = (Path(__file__).parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
CONTEXT = (Path(__file__).parents[1] / "CONTEXT.md").read_text(encoding="utf-8")
AGENTS = (Path(__file__).parents[1] / "AGENTS.md").read_text(encoding="utf-8")
ADR_DIR = Path(__file__).parents[1] / "docs" / "adr"
ADR6 = (ADR_DIR / "0006-unified-interactive-tmux-session.md").read_text(
    encoding="utf-8"
)
ADR9 = (ADR_DIR / "0009-remove-rc-and-background-agent-management.md").read_text(
    encoding="utf-8"
)
CLAUDE_COMPAT = (
    Path(__file__).parents[1] / "docs" / "claude-code-compatibility.md"
).read_text(encoding="utf-8")
RELEASING = (Path(__file__).parents[1] / "docs" / "releasing.md").read_text(
    encoding="utf-8"
)
PYPROJECT = tomllib.loads(
    (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
)
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
    ],
)
def test_readme_cli_examples_are_accepted_by_the_parser(command: str) -> None:
    assert f"csctl {command}" in README
    build_parser().parse_args(shlex.split(command))


@pytest.mark.parametrize(
    "surface",
    [
        README,
        PYPROJECT["project"]["description"],
        cc_session_control.__doc__,
        build_parser().description,
    ],
)
def test_public_descriptions_name_every_supported_provider(surface: str) -> None:
    for provider in ("Claude Code", "Codex CLI", "Kimi Code"):
        assert provider in surface


@pytest.mark.parametrize(
    "retired_command",
    ["csctl prune", "csctl skill", "csctl rc ", "csctl env", "csctl agents"],
)
def test_readme_drops_retired_cli_surfaces(retired_command: str) -> None:
    assert retired_command not in README
    assert retired_command not in CLAUDE


@pytest.mark.parametrize(
    "variable",
    [
        "CSCTL_CLEANUP_AGE_DAYS",
        "CSCTL_THEME",
    ],
)
def test_readme_lists_every_public_environment_setting(variable: str) -> None:
    assert f"`{variable}`" in README


def test_current_knowledge_surfaces_describe_the_unified_tmux_session() -> None:
    assert "share the tmux session named `csctl`" in README
    assert "single tmux session named `csctl`" in CONTEXT
    assert "`csctl` tmux session" in AGENTS
    assert '`cfg.tmux_session == "csctl"`' in CLAUDE
    assert (
        "Every agent session csctl dispatches uses one tmux session named `csctl`"
        in ADR6
    )


def test_unified_tmux_adr_preserves_legacy_residency_after_rc_removal() -> None:
    assert "already resident in any tmux session is entered in place" in ADR6
    assert "ADR-0006" in ADR9
    assert "CSCTL_RC_SESSION" in ADR9


@pytest.mark.parametrize("number", range(1, 8))
def test_removal_adr_supersedes_every_affected_prior_adr(number: int) -> None:
    name = f"ADR-{number:04d}"
    prior_path = next(ADR_DIR.glob(f"{number:04d}-*.md"))

    assert name in ADR9
    assert "ADR-0009" in prior_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "retired_contract",
    [
        "~/.claude/jobs/<short>/state.json",
        "claude remote-control --help",
        "Remote Control help and exit contract",
        "options used by csctl",
    ],
)
def test_compatibility_checklist_drops_retired_management_contracts(
    retired_contract: str,
) -> None:
    assert retired_contract not in CLAUDE_COMPAT


@pytest.mark.parametrize(
    "settled_term",
    [
        "`transcripts.py`",
        "`sessions.scan_result(inputs)`",
        "`resolve_execution_session`",
        "`tmux.residency_inventory`",
        "`Session.tmux_inventory_complete`",
        "`Session.tmux_inventory_detail`",
        "ASCII `?`",
        "`_tmux_run_result`",
        "`tmux.run_in_tmux_result`",
        "`tmux_outcomes.py`",
        "`take_over_result`",
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
        "`respawn_result`",
        "`prepare_takeover`",
        "`remove_job`",
        "`stop_job_result`",
        "`agent_ops`",
        "`registry.read_agent_jobs`",
        "`read_agent_jobs`",
        "`AgentJob`",
        "`enrich_jobs`",
        "`agent_jobs`",
        "`scan_rc_server_inventory`",
        "`scan_servers_result`",
        "`_match_rc_cmdline`",
        "`RCProject`",
        "`RCServer`",
        "`rc_outcomes`",
        "`CSCTL_RC_SESSION`",
        "`split_env_id`",
        "`_tmux_window_inventory`",
        "`_window_for_inventory`",
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
        "`KillResult`",
        "RC 管理与 cleanup 是 TUI 专属表面",
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


def test_release_docs_gate_immutable_tags_on_green_master_candidates() -> None:
    assert "wait for the `CI` workflow" in RELEASING
    assert "SHA to finish successfully" in RELEASING
    assert "git push origin refs/tags/v0.4.1" in RELEASING
    assert "git push origin master --tags" not in RELEASING
    assert "never move or reuse" in RELEASING


def test_session_rescue_does_not_advertise_a_companion_skill() -> None:
    for surface in (README, CLAUDE):
        assert "claude-session-doctor" not in surface
