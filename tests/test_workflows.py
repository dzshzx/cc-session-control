import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PINNED_USE = re.compile(r"^\s*uses:\s*[\w.-]+/[\w.-]+@([0-9a-f]{40})\s+#\s+(\S+)\s*$")


def _load(name: str) -> dict[str, Any]:
    workflow = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _run_commands(job: dict[str, Any]) -> str:
    return "\n".join(str(step["run"]) for step in job["steps"] if "run" in step)


def test_ci_has_quality_test_matrix_and_one_build_job() -> None:
    jobs = _load("ci.yml")["jobs"]

    assert set(jobs) == {"quality", "test", "build"}
    assert jobs["test"]["strategy"]["fail-fast"] is False
    assert jobs["test"]["strategy"]["matrix"]["python-version"] == [
        "3.12",
        "3.13",
        "3.14",
    ]

    quality = _run_commands(jobs["quality"])
    assert "ruff check" in quality
    assert "ruff format --check" in quality
    assert "mypy src/" in quality
    assert "scripts/check_file_sizes.py" in quality
    assert "'/home/' src/" in quality

    tests = _run_commands(jobs["test"])
    assert "--cov-branch" in tests
    assert "--cov-report=json" in tests
    assert "scripts/check_coverage.py" in tests
    assert "uv build" not in tests

    build = _run_commands(jobs["build"])
    assert "uv build --no-sources" in build
    assert "dist/*.whl" in build
    assert "dist/*.tar.gz" in build


def test_every_external_action_is_pinned_to_a_tagged_commit() -> None:
    uses_lines = [
        line
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("uses:")
    ]

    assert uses_lines
    for line in uses_lines:
        assert PINNED_USE.match(line), line
