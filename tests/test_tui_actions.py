"""Typed TUI-action adapters preserve domain outcomes on frozen models."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from cc_session_control.actions import tui_actions
from cc_session_control.actions.runner import (
    Accepted,
    ActionResult,
    ActionRunner,
)
from cc_session_control.data import proc
from cc_session_control.data.project_settings import (
    SettingWriteFailure,
    SettingWriteResult,
    SettingWriteState,
)
from cc_session_control.data.rc import StartResult, StartState
from cc_session_control.data.removal import (
    CleanupExecution,
    CleanupIssue,
    PathRemoval,
    RemovalStatus,
)
from cc_session_control.models import AgentJob, Session


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


def _job() -> AgentJob:
    return AgentJob(
        short="short",
        sid="sid-1",
        resume_sid="resume-1",
        cwd="/tmp/project",
        name="worker",
        respawn_flags=["--model", "opus"],
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
        tui_actions.session_ops.sessions,
        "scan_result",
        lambda _inputs: tui_actions.session_ops.sessions.SessionScanResult((session,)),
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

    result = tui_actions.copy_resume_command(session)

    assert "不会终止原进程" in result.message


def test_copy_resume_command_reports_clipboard_failure(monkeypatch) -> None:
    monkeypatch.setattr(tui_actions.session_ops, "to_clipboard", lambda _cmd: False)

    result = tui_actions.copy_resume_command(_session())

    assert result.message.startswith("复制失败")


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
        tui_actions.session_ops.liveness,
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
        tui_actions.session_ops.liveness,
        "liveness_inputs",
        lambda: tui_actions.session_ops.liveness.LivenessSnapshot(),
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
        tui_actions.session_ops.liveness,
        "liveness_inputs",
        lambda: tui_actions.session_ops.liveness.LivenessSnapshot(),
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
        tui_actions.session_ops.liveness,
        "liveness_inputs",
        lambda: tui_actions.session_ops.liveness.LivenessSnapshot(),
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


def test_start_and_setting_failures_remain_typed(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_actions.rc,
        "start_one_result",
        lambda _path: StartResult(StartState.TRUST_UNAVAILABLE, "/project"),
    )
    start = tui_actions.start_project("/project", "project")
    assert start.message == "项目设置不可用 — 已拒绝启动"

    monkeypatch.setattr(
        tui_actions.rc,
        "start_one_result",
        lambda _path: StartResult(
            StartState.INVENTORY_UNAVAILABLE,
            "/project",
            "tmux list-windows: lost server connection",
        ),
    )
    inventory = tui_actions.start_project("/project", "project")
    assert inventory.message == (
        "RC 清单不可用 — 已拒绝启动：tmux list-windows: lost server connection"
    )

    monkeypatch.setattr(
        tui_actions,
        "write_rc_at_startup",
        lambda *_: SettingWriteResult(
            SettingWriteState.FAILED,
            Path("/project/.claude/settings.local.json"),
            SettingWriteFailure.WRITE,
            "read only",
        ),
    )
    setting = tui_actions.write_auto_rc("/project", "project", True)
    assert setting.message == "配置写入失败（write）: read only"


def test_project_start_reports_partial_metadata_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_actions.rc,
        "start_one_result",
        lambda _path: StartResult(
            StartState.METADATA_FAILED,
            "/project",
            "window-option: lost server connection",
            target="rc:7",
        ),
    )

    result = tui_actions.start_project("/project", "project")

    assert result.message == (
        "启动不完整：tmux 窗口 rc:7 已创建，但元数据写入失败："
        "window-option: lost server connection"
    )


def test_deleted_project_after_setting_submission_is_a_visible_failure(
    tmp_path,
) -> None:
    project = tmp_path / "deleted-project"
    project.mkdir()
    worker_started = threading.Event()
    run_setting = threading.Event()
    ready = threading.Event()

    def delayed_write() -> ActionResult:
        worker_started.set()
        assert run_setting.wait(1)
        return tui_actions.write_auto_rc(str(project), "project", True)

    runner = ActionRunner(ready.set)
    submitted = runner.submit("project.write-settings", delayed_write)
    assert isinstance(submitted, Accepted)
    assert worker_started.wait(1)
    project.rmdir()
    run_setting.set()
    assert ready.wait(1)

    result = runner.consume_result()
    assert result is not None
    assert result.message.startswith("配置写入失败（create-directory）:")
    assert list(tmp_path.iterdir()) == []


def test_agent_respawn_does_not_claim_success_when_tmux_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_actions.agent_ops,
        "respawn_result",
        lambda _job: tui_actions.agent_ops.RespawnResult(
            "claude --resume resume-1 --bg",
            None,
        ),
    )

    result = tui_actions.respawn_agent(_job())

    assert result.message == "重启失败：无法创建 tmux 窗口"


def test_agent_respawn_reports_typed_tmux_failure_detail(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_actions.agent_ops.tmux.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["tmux", "has-session"], 5)
        ),
    )

    result = tui_actions.respawn_agent(_job())

    assert result.message == ("重启失败：session-probe: tmux timed out after 5 seconds")


def test_agent_stop_preserves_all_domain_states(monkeypatch) -> None:
    job = _job()
    cases = [
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.STOPPED,
                pid=42,
            ),
            "已发送停止信号（可能残留孤儿进程，请手动确认）",
        ),
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.NOT_RUNNING,
                detail="no live host",
            ),
            "该后台 agent 未在运行",
        ),
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.REFUSED,
                detail="判活证据不完整",
            ),
            "已拒绝停止：判活证据不完整",
        ),
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.FAILED,
                pid=42,
                detail="permission denied",
            ),
            "停止失败：permission denied",
        ),
    ]

    for domain_result, message in cases:
        monkeypatch.setattr(
            tui_actions.agent_ops,
            "stop_job_result",
            lambda _job, result=domain_result: result,
        )
        action = tui_actions.stop_agent(job)
        assert action.message == message
        assert action.needs_refresh is True


def test_project_stop_preserves_all_domain_states(monkeypatch) -> None:
    cases = [
        (
            tui_actions.rc.StopResult(
                tui_actions.rc.StopState.STOPPED,
                "/project",
            ),
            "已停止 project",
        ),
        (
            tui_actions.rc.StopResult(
                tui_actions.rc.StopState.NOT_RUNNING,
                "/project",
            ),
            "未在运行",
        ),
        (
            tui_actions.rc.StopResult(
                tui_actions.rc.StopState.FAILED,
                "/project",
                "lost server connection",
            ),
            "停止失败：lost server connection",
        ),
    ]

    for domain_result, message in cases:
        monkeypatch.setattr(
            tui_actions.rc,
            "stop_one_result",
            lambda _path, result=domain_result: result,
        )
        action = tui_actions.stop_project("/project", "project")
        assert action.message == message
        assert action.needs_refresh is True


def test_stop_all_projects_preserves_all_domain_states(monkeypatch) -> None:
    cases = [
        (
            tui_actions.rc.StopAllResult(
                tui_actions.rc.StopState.STOPPED,
                "rc",
            ),
            "已停止全部",
        ),
        (
            tui_actions.rc.StopAllResult(
                tui_actions.rc.StopState.NOT_RUNNING,
                "rc",
            ),
            "本来就没在跑",
        ),
        (
            tui_actions.rc.StopAllResult(
                tui_actions.rc.StopState.FAILED,
                "rc",
                "timed out",
            ),
            "停止全部失败：timed out",
        ),
    ]

    for domain_result, message in cases:
        monkeypatch.setattr(
            tui_actions.rc,
            "stop_all_result",
            lambda result=domain_result: result,
        )
        action = tui_actions.stop_all_projects()
        assert action.message == message
        assert action.needs_refresh is True
