"""Public CLI entry/dispatch behavior, the resume command, theme
flag wiring, and the TUI exit-intent handoff."""

from __future__ import annotations

import io
import subprocess
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from cc_session_control import cli
from cc_session_control.actions import session_ops
from cc_session_control.config import cfg
from cc_session_control.data import (
    liveness,
    registry,
    sessions,
)
from cc_session_control.models import Session


def _session(sid: str, label: str) -> Session:
    return Session(
        sid=sid,
        cwd="/project",
        label=label,
        mtime=1,
        prompts=1,
        pid=None,
        alive=False,
        current=False,
    )


def test_main_accepts_argv_and_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(),
    )

    assert cli.main(["resume"]) == 0
    assert capsys.readouterr().out == "No matching sessions.\n"


@pytest.mark.parametrize("argv", [["unknown"], ["prune"], ["skill"], ["rc", "status"]])
def test_unknown_command_is_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)

    assert stopped.value.code == 2


def test_dispatch_rejects_namespace_without_a_registered_handler() -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.dispatch(
            Namespace(command="unregistered"),
            cli.build_parser(),
        )

    assert stopped.value.code == 2


def test_handler_streams_are_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    args = cli.build_parser().parse_args(["resume"])

    status = cli.dispatch(
        args,
        cli.build_parser(),
        stdout=stdout,
        stderr=stderr,
    )

    assert status == 0
    assert stdout.getvalue() == "No matching sessions.\n"
    assert stderr.getvalue() == ""


def test_resume_keyword_page_limit_and_all_reach_public_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transcript = tmp_path / "body-match.jsonl"
    transcript.write_text('{"text": "apple from transcript body"}\n')
    body_match = replace(_session("sid-two", "metadata miss"), file=str(transcript))
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(
            (
                _session("sid-one", "apple metadata"),
                body_match,
                _session("sid-three", "banana"),
            )
        ),
    )

    assert (
        cli.main(
            ["resume", "apple", "--page", "2", "--limit", "1"],
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "sid-two" in output
    assert "sid-one" not in output
    assert "-- page 2/2, 2 session(s) --" in output

    assert cli.main(["resume", "apple", "--all"]) == 0
    output = capsys.readouterr().out
    assert "sid-one" in output
    assert "sid-two" in output
    assert "sid-three" not in output
    assert "-- 2 session(s) --" in output


def test_resume_incomplete_transcript_scan_emits_no_inventory_or_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issue = sessions.TranscriptIssue(
        "session transcript",
        "/runtime/projects/project/unreadable.jsonl",
        "permission denied",
    )
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(
            (_session("unsafe", "partial inventory"),),
            (issue,),
        ),
    )

    assert cli.main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "transcript inventory is incomplete" in captured.err
    assert issue.source in captured.err
    assert issue.path in captured.err
    assert issue.detail in captured.err
    assert "claude --resume" not in captured.err


def test_resume_keyword_body_read_race_is_a_no_match_not_a_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A transcript deleted between inventory and body search (e.g. a race,
    or a provider like kimi whose index outlives manually removed session
    dirs) degrades that one row to "no match" — it must not refuse the
    whole search."""
    transcript = tmp_path / "disappeared.jsonl"
    transcript.write_text('{"text": "needle"}\n')
    session = replace(_session("raced", "metadata miss"), file=str(transcript))
    transcript.unlink()
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult((session,)),
    )

    assert cli.main(["resume", "needle"]) == 0
    captured = capsys.readouterr()
    assert "No matching sessions" in captured.out
    assert captured.err == ""
    assert "claude --resume" not in captured.out


@pytest.mark.parametrize(
    ("source_kind", "expected_source", "expected_path", "expected_detail"),
    [
        (
            "session",
            "session registry",
            "sessions/broken.json",
            "invalid schema",
        ),
        (
            "agents",
            "claude agents --json",
            "claude agents --json",
            "invalid JSON",
        ),
    ],
)
def test_resume_malformed_or_unreadable_liveness_emits_no_actionable_command(
    source_kind: str,
    expected_source: str,
    expected_path: str,
    expected_detail: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    registry.invalidate_cache()
    liveness.invalidate_cache()
    monkeypatch.setattr(liveness.proc, "has_proc", lambda: False)
    monkeypatch.setattr(liveness.proc, "ancestor_pids", lambda: set())
    if source_kind == "session":
        broken = cfg.sessions_dir / "broken.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{}")
    agents_stdout = "{bad json" if source_kind == "agents" else "[]"
    monkeypatch.setattr(
        liveness.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=agents_stdout,
            stderr="",
        ),
    )
    actual_inputs = liveness.liveness_inputs
    snapshots = 0
    scans = 0

    def capture_evidence() -> liveness.LivenessSnapshot:
        nonlocal snapshots
        snapshots += 1
        return actual_inputs()

    def reject_scan(
        inputs: liveness.LivenessSnapshot,
    ) -> sessions.SessionScanResult:
        nonlocal scans
        scans += 1
        return sessions.SessionScanResult((_session("unsafe", "must not render"),))

    monkeypatch.setattr(liveness, "liveness_inputs", capture_evidence)
    monkeypatch.setattr(sessions, "scan_result", reject_scan)

    assert cli.main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "liveness evidence is incomplete" in captured.err
    assert expected_source in captured.err
    assert expected_path in captured.err
    assert expected_detail in captured.err
    assert "claude --resume" not in captured.err
    assert snapshots == 1
    assert scans == 0


def test_resume_complete_liveness_is_injected_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = liveness.LivenessSnapshot()
    snapshots = 0
    injected = []

    def capture_evidence() -> liveness.LivenessSnapshot:
        nonlocal snapshots
        snapshots += 1
        return evidence

    def capture_scan(
        inputs: liveness.LivenessSnapshot,
    ) -> sessions.SessionScanResult:
        injected.append(inputs)
        return sessions.SessionScanResult((_session("safe", "complete evidence"),))

    monkeypatch.setattr(liveness, "liveness_inputs", capture_evidence)
    monkeypatch.setattr(sessions, "scan_result", capture_scan)

    assert cli.main(["resume"]) == 0
    captured = capsys.readouterr()
    assert "claude --resume safe" in captured.out
    assert captured.err == ""
    assert snapshots == 1
    assert injected == [evidence]


def test_no_command_runs_tui_and_handles_no_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cc_session_control import app as app_mod

    events: list[str] = []

    class FakeApp:
        def run(self) -> None:
            events.append("loop")
            return None

    monkeypatch.setattr(app_mod, "App", FakeApp)

    assert cli.main([]) == 0
    assert events == ["loop"]


@pytest.mark.parametrize(
    ("intent_type", "intent"),
    [
        (
            session_ops.ResumeIntent,
            session_ops.ResumeIntent(_session("resume", "resume")),
        ),
        (
            session_ops.AttachIntent,
            session_ops.AttachIntent("project:1"),
        ),
        (
            session_ops.TmuxResumeIntent,
            session_ops.TmuxResumeIntent(_session("tmux", "tmux")),
        ),
        (
            session_ops.TmuxNewIntent,
            session_ops.TmuxNewIntent("/project"),
        ),
    ],
)
def test_every_exit_intent_finalizes_once_after_tui_loop(
    intent_type: type[session_ops.ExitIntent],
    intent: session_ops.ExitIntent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cc_session_control import app as app_mod

    events: list[str] = []

    class FakeApp:
        def run(self) -> session_ops.ExitIntent:
            events.append("loop")
            return intent

    monkeypatch.setattr(app_mod, "App", FakeApp)
    monkeypatch.setattr(
        intent_type,
        "run",
        lambda _self: events.append("intent") or 0,
    )

    assert cli.main([]) == 0
    assert events == ["loop", "intent"]


def test_theme_flag_sets_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli.build_parser().parse_args(["--theme", "light"])
    cli.apply_global_flags(args)
    assert cfg.theme == "light"


def test_theme_flag_absent_keeps_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli.build_parser().parse_args([])
    cli.apply_global_flags(args)
    assert cfg.theme == "auto"


def test_invalid_theme_is_argparse_error() -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--theme", "neon"])

    assert stopped.value.code == 2
