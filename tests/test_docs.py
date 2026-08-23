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

REPO = Path(__file__).parents[1]
README = (REPO / "README.md").read_text(encoding="utf-8")
CLAUDE = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
# Architecture reference doc: holds the typed-seam terminology gated by
# test_architecture_doc_uses_settled_typed_seams (moved out of CLAUDE.md in
# 581d354).
ARCH = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8")
CONTEXT = (REPO / "CONTEXT.md").read_text(encoding="utf-8")
AGENTS = (REPO / "AGENTS.md").read_text(encoding="utf-8")
ADR_DIR = REPO / "docs" / "adr"
# Surfaces that state CURRENT knowledge. ADR bodies are deliberately absent:
# they are the record of what was retired and must keep naming it (ADR-0009
# names `CSCTL_RC_SESSION`, ADR-0004 the removed subcommands); only the ADR
# index is a navigation surface.
CURRENT_KNOWLEDGE_SURFACES = {
    path.relative_to(REPO).as_posix(): path.read_text(encoding="utf-8")
    for path in [
        *sorted((REPO / "docs").glob("*.md")),
        ADR_DIR / "index.md",
        *(
            REPO / name
            for name in (
                "CLAUDE.md",
                "AGENTS.md",
                "CONTEXT.md",
                "README.md",
                "CONTRIBUTING.md",
            )
        ),
    ]
}
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
# Reusable local workflows callable via `uses: ./...`. Both are shared quality
# gates (scripts/check.sh; the 3.13/3.14 pytest matrix), so being local is not
# a pinning gap the way an external `owner/repo@ref` reference would be.
ALLOWED_LOCAL_WORKFLOW_USES = {
    "./.github/workflows/quality-gate.yml",
    "./.github/workflows/test-matrix.yml",
}
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
        CONTEXT,
        PYPROJECT["project"]["description"],
        cc_session_control.__doc__,
        build_parser().description,
    ],
)
def test_public_descriptions_name_every_supported_provider(surface: str) -> None:
    for provider in ("Claude Code", "Codex CLI", "Kimi Code", "opencode"):
        assert provider in surface


@pytest.mark.parametrize(
    "variable",
    [
        "CSCTL_CLEANUP_AGE_DAYS",
        "CSCTL_THEME",
    ],
)
def test_readme_lists_every_public_environment_setting(variable: str) -> None:
    assert f"`{variable}`" in README


# Match the identifiers, not one sentence: the docs may rephrase freely as
# long as they still name the single `csctl` tmux session.
_UNIFIED_TMUX_SESSION = re.compile(
    r"tmux session (named )?`csctl`|`csctl` tmux session"
)


def test_current_knowledge_surfaces_describe_the_unified_tmux_session() -> None:
    for surface in (README, CONTEXT, AGENTS, ADR6):
        assert _UNIFIED_TMUX_SESSION.search(surface)
    assert '`cfg.tmux_session == "csctl"`' in ARCH
    # 2026-08-23: windows are named for the bare CLI, never the project or sid.
    assert "`claude`/`codex`/`kimi`/`opencode`" in README


def test_unified_tmux_adr_preserves_legacy_residency_after_rc_removal() -> None:
    assert "already resident in any tmux session is entered in place" in ADR6
    assert "ADR-0006" in ADR9
    assert "CSCTL_RC_SESSION" in ADR9


_ADR_RELATION = re.compile(
    r"\b(supersed\w*|extend\w*|amend\w*|narrow\w*|resolves)\b[^.\n]{0,160}?ADR-(\d{4})",
    re.IGNORECASE,
)


def _adr_status_block(text: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Status:"))
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return "\n".join(lines[start:end])


def _declared_adr_relations() -> list[tuple[str, str]]:
    """(later ADR, earlier ADR) pairs for every supersedes/extends/amends declaration."""
    pairs: list[tuple[str, str]] = []
    for path in sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")):
        own = path.name[:4]
        for match in _ADR_RELATION.finditer(path.read_text(encoding="utf-8")):
            target = match.group(2)
            if target < own:
                pairs.append((own, target))
    return sorted(set(pairs))


@pytest.mark.parametrize("later, earlier", _declared_adr_relations())
def test_every_declared_adr_relation_has_a_status_back_pointer(
    later: str, earlier: str
) -> None:
    # Audit 2026-08-22: 0005/0007/0010/0011 changed earlier ADRs without the earlier
    # file's Status saying so; a reader opening one ADR must see every later change.
    earlier_path = next(ADR_DIR.glob(f"{earlier}-*.md"))
    assert f"ADR-{later}" in _adr_status_block(
        earlier_path.read_text(encoding="utf-8")
    ), f"ADR-{earlier} Status must point at ADR-{later}"


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
def test_architecture_doc_uses_settled_typed_seams(settled_term: str) -> None:
    assert settled_term in ARCH


# Reverse assertions ("this term is gone") only earn their place while the
# term has a live resurrection path; each entry names that path. Anything
# without one is covered by the positive settled-seam test above instead.
RETIRED_TERMS = [
    # ADR-0004/0009 removed the headless subcommands; old agent-facing usage
    # blocks and skills still quote them and get pasted back into README/CLAUDE.
    pytest.param(r"csctl (prune|env|skill|rc|agents)\b", id="headless-subcommands"),
    # Retired tmux-session override (ADR-0006/0009); the README config table is
    # exactly where someone re-adds it from memory.
    pytest.param(r"CSCTL_RC_SESSION", id="env-var"),
    # Window names are bare CLI names since the 2026-08-23 ADR-0005/0006
    # amendments; master docs still carried both older schemes until 9ca8c01.
    pytest.param(r"<project>/<leaf>", id="project-leaf-window-names"),
    pytest.param(r"\b(cx|km|oc)-<sid8>", id="sid-bearing-window-names"),
    # The bridge-environment ledger pipeline dropped in 0.8 (ADR-0004) is the
    # most extensively documented dead feature in git history.
    pytest.param(r"environments\.jsonl", id="bridge-env-ledger"),
    # The companion skill moved to agent-skills in 0.8.0; README/CLAUDE used to
    # advertise it next to `csctl resume`.
    pytest.param(r"claude-session-doctor", id="companion-skill"),
    # CONTRIBUTING replaced the line cap with a design-signal rule; commit
    # messages and old memories still say "all files <600 lines".
    pytest.param(r"\b600[ -]?(行|lines?)", id="file-size-line-budget"),
    # 0.8.5 kimi backfill watch removed by the ADR-0005 2026-08-12 amendment;
    # the kimi binding story is the most rewritten part of the docs.
    pytest.param(r"_bind-window", id="kimi-backfill-watch"),
]


@pytest.mark.parametrize("pattern", RETIRED_TERMS)
def test_current_knowledge_surfaces_do_not_resurrect_retired_terms(
    pattern: str,
) -> None:
    regex = re.compile(pattern)
    hits = [
        name for name, body in CURRENT_KNOWLEDGE_SURFACES.items() if regex.search(body)
    ]
    assert not hits, f"{pattern!r} resurfaced in {hits}"


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
            assert reference in ALLOWED_LOCAL_WORKFLOW_USES
        else:
            assert PINNED_EXTERNAL_USE.match(line), line


def test_release_docs_gate_immutable_tags_on_green_master_candidates() -> None:
    assert "wait for the `CI` workflow" in RELEASING
    assert "SHA to finish successfully" in RELEASING
    assert "git push origin refs/tags/v0.4.1" in RELEASING
    assert "git push origin master --tags" not in RELEASING
    assert "never move or reuse" in RELEASING
