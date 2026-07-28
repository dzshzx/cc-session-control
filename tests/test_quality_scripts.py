import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast


def _load_script(script_name: str) -> ModuleType:
    script = Path(__file__).parents[1] / "scripts" / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(script_name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_main(script_name: str) -> Callable[[list[str] | None], int]:
    module = _load_script(script_name)
    return cast(Callable[[list[str] | None], int], module.main)


def _write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pass\n" * count, encoding="utf-8")


def test_main_accepts_python_files_at_limit(tmp_path: Path, capsys) -> None:
    source = tmp_path / "src"
    _write_lines(source / "package" / "module.py", 2)

    assert _load_main("check_file_sizes")([str(source), "--max-lines", "2"]) == 0
    assert capsys.readouterr() == ("", "")


def test_main_rejects_python_file_over_limit(tmp_path: Path, capsys) -> None:
    source = tmp_path / "src"
    oversized = source / "package" / "oversized.py"
    _write_lines(oversized, 3)

    assert _load_main("check_file_sizes")([str(source), "--max-lines", "2"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{oversized}: 3 lines exceeds limit 2\n"


def test_main_ignores_non_python_files_and_test_directories(
    tmp_path: Path, capsys
) -> None:
    source = tmp_path / "src"
    _write_lines(source / "package" / "module.py", 1)
    _write_lines(source / "package" / "notes.txt", 3)
    _write_lines(source / "package" / "tests" / "test_large.py", 3)

    assert _load_main("check_file_sizes")([str(source), "--max-lines", "2"]) == 0
    assert capsys.readouterr() == ("", "")


def _write_coverage(path: Path, *, statements: float, branches: float) -> None:
    path.write_text(
        json.dumps(
            {
                "totals": {
                    "percent_statements_covered": statements,
                    "percent_branches_covered": branches,
                }
            }
        ),
        encoding="utf-8",
    )


def test_coverage_ratchet_accepts_results_at_floors(tmp_path: Path, capsys) -> None:
    report = tmp_path / "coverage.json"
    _write_coverage(report, statements=85.0, branches=75.0)

    assert _load_main("check_coverage")([str(report)]) == 0
    assert capsys.readouterr() == ("", "")


def test_coverage_ratchet_reports_each_failed_metric(tmp_path: Path, capsys) -> None:
    report = tmp_path / "coverage.json"
    _write_coverage(report, statements=84.99, branches=74.0)

    assert _load_main("check_coverage")([str(report)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "statement coverage 84.99% is below required 85.00%\n"
        "branch coverage 74.00% is below required 75.00%\n"
    )


def test_bump_version_suggests_an_annotated_release_tag(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    module = _load_script("bump_version")
    monkeypatch.setattr(module, "INIT", init)

    assert module.main(["patch"]) == 0

    assert init.read_text(encoding="utf-8") == '__version__ = "1.2.4"\n'
    assert capsys.readouterr() == (
        "1.2.3 -> 1.2.4\n"
        "next: git commit -am 'chore: bump version to 1.2.4' && "
        "git tag -a v1.2.4 -m 'v1.2.4'\n",
        "",
    )
