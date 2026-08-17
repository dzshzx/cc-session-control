"""Codex app-server fd evidence is hosted/read-only, never liveness."""

from pathlib import Path

from cc_session_control.data.proc import ProcCli, ProcIssue, ProcOpenFileInventory
from cc_session_control.data.providers.codex_hosted import (
    is_app_server,
    scan_hosted_rollouts,
)


def _proc(pid: int, *argv: str, env=None) -> ProcCli:
    return ProcCli(pid, tuple(argv), "100", env=env)


def test_is_app_server_requires_codex_argv_shape():
    assert is_app_server(_proc(1, "codex", "app-server", "--listen", "unix://"))
    assert is_app_server(
        _proc(2, "codex", "-c", "features.code_mode_host=true", "app-server")
    )
    assert not is_app_server(_proc(3, "codex", "resume", "sid"))
    assert not is_app_server(_proc(4, "wrapper", "app-server"))
    assert not is_app_server(_proc(5, "codex", "exec", "app-server"))


def test_scan_hosted_rollouts_keeps_exact_active_paths_for_owned_process(tmp_path):
    home = tmp_path / "codex"
    held = home / "sessions" / "2026" / "08" / "17" / "rollout-held.jsonl"
    foreign = tmp_path / "other" / "sessions" / "rollout-foreign.jsonl"
    archived = home / "archived_sessions" / "rollout-archived.jsonl"
    seen: list[tuple[int, ...]] = []

    def scan(pids):
        seen.append(tuple(pids))
        return ProcOpenFileInventory(frozenset(map(str, (held, foreign, archived))))

    result = scan_hosted_rollouts(
        home,
        "codex sessions",
        (
            _proc(11, "codex", "app-server"),
            _proc(12, "codex", "app-server"),
            _proc(13, "codex", "resume", "sid"),
        ),
        lambda record: record.pid == 11,
        scan_open_files=scan,
    )

    assert seen == [(11,)]
    assert result.paths == frozenset({str(held)})
    assert result.issues == ()


def test_scan_hosted_rollouts_maps_proc_failures_to_provider_issues(tmp_path):
    issue = ProcIssue("process open files", "/proc/11/fd", "permission denied")

    result = scan_hosted_rollouts(
        Path(tmp_path),
        "codex:test sessions",
        (_proc(11, "codex", "app-server"),),
        lambda _record: True,
        scan_open_files=lambda _pids: ProcOpenFileInventory(issues=(issue,)),
    )

    assert result.paths == frozenset()
    assert result.issues == (
        ProcIssue("codex:test sessions", "/proc/11/fd", "permission denied"),
    )
