import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW_NAMES = {
    "ci.yml",
    "quality-gate.yml",
    "release-testpypi.yml",
    "release.yml",
}
QUALITY_GATE_USE = "./.github/workflows/quality-gate.yml"
PINNED_EXTERNAL_USE = re.compile(
    r"^\s*uses:\s*[\w.-]+/[\w.-]+@([0-9a-f]{40})\s+#\s+(\S+)\s*$"
)


def _load(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _run_commands(job: dict[str, Any]) -> str:
    return "\n".join(str(step["run"]) for step in job["steps"] if "run" in step)


def _triggers(workflow: dict[str, Any]) -> Any:
    # PyYAML follows YAML 1.1 and parses the unquoted `on` key as True.
    return workflow.get("on", workflow.get(True))


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def test_all_workflows_parse() -> None:
    assert {path.name for path in WORKFLOWS.glob("*.y*ml")} == WORKFLOW_NAMES
    for name in WORKFLOW_NAMES:
        _load(name)


def test_reusable_quality_gate_has_every_release_blocker() -> None:
    workflow = _load("quality-gate.yml")

    assert _triggers(workflow) == {"workflow_call": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"quality"}

    quality = workflow["jobs"]["quality"]
    assert quality["runs-on"] == "ubuntu-latest"
    python_step = next(
        step for step in quality["steps"] if step.get("name") == "Set up Python"
    )
    assert python_step["with"]["python-version"] == "3.12"

    commands = _run_commands(quality)
    required_commands = [
        "uv run --extra dev ruff check src tests scripts",
        "uv run --extra dev ruff format --check src tests scripts",
        "uv run --extra dev mypy src/",
        "uv run --extra dev python scripts/check_file_sizes.py --tests tests",
        "grep -rn --include='*.py' '/home/' src/",
        "uv run --extra dev pytest tests/",
        "--cov=cc_session_control",
        "--cov-branch",
        "--cov-report=json",
        "uv run --extra dev python scripts/check_coverage.py coverage.json",
        "--statement-floor 91",
        "--branch-floor 82",
    ]
    for command in required_commands:
        assert command in commands


def test_ci_reuses_quality_gate_runs_matrix_and_builds_once() -> None:
    workflow = _load("ci.yml")
    jobs = workflow["jobs"]

    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"quality-gate", "test", "build"}
    assert jobs["quality-gate"] == {
        "permissions": {"contents": "read"},
        "uses": QUALITY_GATE_USE,
    }
    assert jobs["test"]["strategy"]["fail-fast"] is False
    assert jobs["test"]["strategy"]["matrix"]["python-version"] == [
        "3.12",
        "3.13",
        "3.14",
    ]

    tests = _run_commands(jobs["test"])
    assert "uv run --extra dev pytest tests/" in tests
    assert "--cov" not in tests
    assert "scripts/check_coverage.py" not in tests
    assert "uv build" not in tests

    build = _run_commands(jobs["build"])
    assert _needs(jobs["build"]) == {"quality-gate", "test"}
    assert "uv build --no-sources" in build
    assert "dist/*.whl" in build
    assert "dist/*.tar.gz" in build
    assert (
        sum(
            "uv build" in str(step.get("run", ""))
            for job in jobs.values()
            for step in job.get("steps", [])
        )
        == 1
    )


def test_testpypi_requires_quality_gate_before_publish() -> None:
    workflow = _load("release-testpypi.yml")
    jobs = workflow["jobs"]

    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"quality-gate", "publish-testpypi"}
    assert jobs["quality-gate"] == {
        "permissions": {"contents": "read"},
        "uses": QUALITY_GATE_USE,
    }

    publish = jobs["publish-testpypi"]
    assert _needs(publish) == {"quality-gate"}
    assert publish["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert "uv build --no-sources" in _run_commands(publish)


def test_production_release_validates_tag_before_publish() -> None:
    workflow = _load("release.yml")
    jobs = workflow["jobs"]

    assert workflow["permissions"] == {"contents": "read"}
    assert set(jobs) == {"quality-gate", "validate-release-tag", "publish"}
    assert jobs["quality-gate"] == {
        "permissions": {"contents": "read"},
        "uses": QUALITY_GATE_USE,
    }

    validation = jobs["validate-release-tag"]
    assert validation["permissions"] == {"contents": "read"}
    assert validation["runs-on"] == "ubuntu-latest"
    checkout = next(
        step
        for step in validation["steps"]
        if step.get("name") == "Check out repository"
    )
    assert checkout["with"]["fetch-depth"] == 0
    assert any(step.get("name") == "Set up Python" for step in validation["steps"])
    assert 'python scripts/validate_release_tag.py "$GITHUB_REF_NAME"' in _run_commands(
        validation
    )

    publish = jobs["publish"]
    assert _needs(publish) == {"quality-gate", "validate-release-tag"}
    assert publish["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert "uv build --no-sources" in _run_commands(publish)


def test_production_release_is_tag_only_and_testpypi_is_manual() -> None:
    assert _triggers(_load("release.yml")) == {"push": {"tags": ["v*"]}}
    assert _triggers(_load("release-testpypi.yml")) == {"workflow_dispatch": None}


def test_every_external_action_is_pinned_to_a_tagged_commit() -> None:
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
