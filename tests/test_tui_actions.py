"""Typed TUI-action adapters preserve domain outcomes on frozen models."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cc_session_control.actions import execution_target, tui_actions
from cc_session_control.data import proc
from cc_session_control.data.removal import (
    CleanupExecution,
    CleanupIssue,
    PathRemoval,
    RemovalStatus,
)
from cc_session_control.models import Session


def _session() -> Session:
    return Session(
        sid="sid-1",
        cwd="/tmp/project",
        label="session",
        mtime=1.0,
        prompts=2,
        pid=42,
        alive=True,
        current=False,
        proc_start="100",
        file="/tmp/session.jsonl",
    )


def _created_target(target: str) -> tui_actions.session_ops.tmux.TmuxWriteResult:
    tmux = tui_actions.session_ops.tmux
    return tmux.TmuxWriteResult(
        tmux.TmuxWriteStage.NEW_WINDOW,
        tmux.TmuxWriteState.SUCCEEDED,
        target=target,
    )


def _install_execution_session(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    monkeypatch.setattr(
        execution_target.sessions,
        "scan_result",
        lambda _inputs: execution_target.sessions.SessionScanResult((session,)),
    )


def test_copy_resume_command_notifies_plainly_for_claude(monkeypatch) -> None:
    monkeypatch.setattr(tui_actions.session_ops, "to_clipboard", lambda _cmd: True)

    result = tui_actions.copy_resume_command(_session())

    assert result.message == "已复制"


def test_copy_resume_command_notes_no_takeover_for_non_claude_live(
    monkeypatch,
) -> None:
    # ADR-0005: a non-Claude live row copies a direct provider resume
    # command, not a Claude-only guarded takeover — the notice must not
    # claim the copied command stops the running process.
    monkeypatch.setattr(tui_actions.session_ops, "to_clipboard", lambda _cmd: True)
    session = replace(_session(), provider="codex")
    monkeypatch.setattr(
        tui_actions.session_ops,
        "session_for_execution",
        lambda _session, fork: execution_target.ExecutionSessionResolution(
            execution_target.ExecutionSessionState.RESOLVED,
            session=session,
        ),
    )

    result = tui_actions.copy_resume_command(session)

    assert "不会终止原进程" in result.message


def test_copy_resume_command_reports_clipboard_failure(monkeypatch) -> None:
    monkeypatch.setattr(tui_actions.session_ops, "to_clipboard", lambda _cmd: False)

    result = tui_actions.copy_resume_command(_session())

    assert result.message.startswith("复制失败")


@pytest.mark.parametrize(
    "action",
    [
        tui_actions.stop_session,
        tui_actions.background_session,
        tui_actions.delete_session,
        tui_actions.copy_resume_command,
    ],
)
def test_hosted_session_actions_are_read_only(action) -> None:
    result = action(replace(_session(), provider="codex", alive=False, hosted=True))

    assert "托管会话只读" in result.message
    assert result.needs_refresh is False


def test_copy_codex_command_refreshes_and_refuses_new_hosting(monkeypatch) -> None:
    snapshot = replace(_session(), provider="codex", alive=False)
    monkeypatch.setattr(
        tui_actions.session_ops,
        "session_for_execution",
        lambda _session, fork: execution_target.ExecutionSessionResolution(
            execution_target.ExecutionSessionState.REFUSED,
            detail="session is app-server hosted and read-only",
        ),
    )

    result = tui_actions.copy_resume_command(snapshot)

    assert "复制失败" in result.message
    assert "hosted and read-only" in result.message
    assert result.needs_refresh


def test_stop_session_preserves_refusal_and_failure(monkeypatch) -> None:
    session = _session()
    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda *_: tui_actions.session_ops.TakeOverOutcome(
            tui_actions.session_ops.TakeOverState.REFUSED,
        ),
    )
    refused = tui_actions.stop_session(session)
    assert "liveness 降级" in refused.message

    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda *_: tui_actions.session_ops.TakeOverOutcome(
            tui_actions.session_ops.TakeOverState.FAILED,
        ),
    )
    failed = tui_actions.stop_session(session)
    assert failed.message == "停止失败"


def test_stop_session_refuses_unknown_proc_probe_without_signal(monkeypatch) -> None:
    session = _session()
    issue = proc.ProcIssue(
        "process stat",
        "/proc/42/stat",
        "permission denied",
    )
    monkeypatch.setattr(
        proc,
        "probe_current_ancestors",
        lambda: proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(
        proc,
        "probe_pid",
        lambda pid, start: proc.PidProbe(pid, None, issue=issue),
    )
    monkeypatch.setattr(
        tui_actions.session_ops.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    result = tui_actions.stop_session(session)

    assert "/proc/42/stat" in result.message
    assert "permission denied" in result.message


def test_dead_background_session_skips_liveness_and_reaches_tmux(monkeypatch) -> None:
    session = replace(_session(), alive=False, pid=None)
    spawn_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        execution_target.liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(AssertionError("must not acquire liveness")),
    )
    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(
        tui_actions.session_ops.tmux,
        "run_in_tmux_result",
        lambda tmux_session, window, cmd, **_kwargs: (
            spawn_calls.append((tmux_session, window, cmd))
            or _created_target("csctl:4")
        ),
    )

    result = tui_actions.background_session(session)

    assert result.message == "已转入后台（tmux csctl:4）"
    assert result.needs_refresh is True
    assert spawn_calls == [
        ("csctl", "project/sid-1", "cd /tmp/project && claude --resume sid-1")
    ]


@pytest.mark.parametrize(
    ("takeover", "expected_message", "expected_spawns"),
    [
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.REFUSED,
                "ancestor chain indeterminate",
            ),
            "转入后台失败：ancestor chain indeterminate",
            [],
        ),
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.FAILED,
                "permission denied",
            ),
            "转入后台失败：permission denied",
            [],
        ),
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.KILLED,
            ),
            "已转入后台（tmux csctl:4）",
            [("csctl", "project/sid-1", "cd /tmp/project && claude --resume sid-1")],
        ),
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.GONE,
            ),
            "已转入后台（tmux csctl:4）",
            [("csctl", "project/sid-1", "cd /tmp/project && claude --resume sid-1")],
        ),
    ],
)
def test_live_background_session_requires_successful_takeover_before_spawn(
    takeover: tui_actions.session_ops.TakeOverOutcome,
    expected_message: str,
    expected_spawns: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    spawn_calls: list[tuple[str, str, str]] = []
    _install_execution_session(monkeypatch, session)
    monkeypatch.setattr(
        execution_target.liveness,
        "liveness_inputs",
        lambda: execution_target.liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda *_args: takeover,
    )
    monkeypatch.setattr(
        tui_actions.session_ops.os.path,
        "isdir",
        lambda _path: True,
    )
    monkeypatch.setattr(
        tui_actions.session_ops.tmux,
        "run_in_tmux_result",
        lambda tmux_session, window, cmd, **_kwargs: (
            spawn_calls.append((tmux_session, window, cmd))
            or _created_target("csctl:4")
        ),
    )

    result = tui_actions.background_session(session)

    assert result.message == expected_message
    assert spawn_calls == expected_spawns


def test_live_background_session_without_pid_fails_closed_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = replace(_session(), pid=None)
    _install_execution_session(monkeypatch, session)
    monkeypatch.setattr(
        execution_target.liveness,
        "liveness_inputs",
        lambda: execution_target.liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not take over")),
    )
    monkeypatch.setattr(
        tui_actions.session_ops.os.path,
        "isdir",
        lambda _path: True,
    )
    monkeypatch.setattr(
        tui_actions.session_ops.tmux,
        "run_in_tmux_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not spawn")
        ),
    )

    result = tui_actions.background_session(session)

    assert "incomplete execution-time identity (pid)" in result.message


def test_live_background_session_uses_execution_time_session_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _session()
    fresh = replace(
        stale,
        cwd="/fresh-project",
        pid=9002,
        proc_start="fresh-start",
    )
    _install_execution_session(monkeypatch, fresh)
    monkeypatch.setattr(
        execution_target.liveness,
        "liveness_inputs",
        lambda: execution_target.liveness.LivenessSnapshot(),
    )
    takeovers: list[tuple[int, str]] = []
    spawns: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda pid, start: (
            takeovers.append((pid, start))
            or tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.KILLED
            )
        ),
    )
    monkeypatch.setattr(
        tui_actions.session_ops.os.path,
        "isdir",
        lambda _path: True,
    )
    monkeypatch.setattr(
        tui_actions.session_ops.tmux,
        "run_in_tmux_result",
        lambda tmux_session, window, cmd, **_kwargs: (
            spawns.append((tmux_session, window, cmd)) or _created_target("csctl:4")
        ),
    )

    result = tui_actions.background_session(stale)

    assert result.message == "已转入后台（tmux csctl:4）"
    assert takeovers == [(9002, "fresh-start")]
    assert spawns == [
        (
            "csctl",
            "fresh-project/sid-1",
            "cd /fresh-project && claude --resume sid-1",
        )
    ]


def test_cleanup_adapter_reports_partial_and_refused() -> None:
    partial = CleanupExecution(
        removals=[
            PathRemoval(Path("/a"), RemovalStatus.REMOVED),
            PathRemoval(Path("/b"), RemovalStatus.FAILED, "denied"),
        ],
        completed=["one"],
    )
    partial_result = tui_actions.run_cleanup(
        lambda _targets: partial,
        ("one",),
        "已清理 {n} 项",
    )
    assert partial_result.message.startswith("部分完成：")

    refused = CleanupExecution()
    refused.refuse(["one"], "current session cannot be determined")
    refused_result = tui_actions.run_cleanup(
        lambda _targets: refused,
        ("one",),
        "已清理 {n} 项",
    )
    assert refused_result.message.startswith("已拒绝清理：")


def test_delete_adapter_reports_removed_plus_anchor_refusal_as_partial(
    monkeypatch,
) -> None:
    execution = CleanupExecution(completed=["sid-1"])
    execution.add_removal(PathRemoval(Path("/safe"), RemovalStatus.REMOVED))
    execution.add_removal(
        PathRemoval(
            Path("/changed"),
            RemovalStatus.REFUSED,
            "anchored root identity changed after preview",
        )
    )
    monkeypatch.setattr(tui_actions.cleanup, "remove_session", lambda _: execution)

    result = tui_actions.delete_session(_session())

    assert result.message.startswith("删除部分完成：")
    assert "anchored root identity changed" in result.message


def test_cleanup_adapter_reports_incomplete_liveness_in_chinese() -> None:
    refused = CleanupExecution(
        issues=[
            CleanupIssue(
                source="session registry",
                path="/runtime/sessions/broken.json",
                error="invalid JSON",
            )
        ]
    )
    refused.refuse(["one"], "liveness evidence incomplete; nothing deleted")

    result = tui_actions.run_cleanup(
        lambda _targets: refused,
        ("one",),
        "已清理 {n} 项",
    )

    assert "保护证据不完整，未删除" in result.message
    assert "session registry" in result.message
    assert "/runtime/sessions/broken.json" in result.message
    assert "invalid JSON" in result.message
