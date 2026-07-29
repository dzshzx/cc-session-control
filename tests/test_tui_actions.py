"""Typed TUI-action adapters preserve domain outcomes and request snapshots."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from cc_session_control.actions import tui_actions
from cc_session_control.actions.runner import (
    Accepted,
    ActionResult,
    ActionRunner,
    ActionStatus,
)
from cc_session_control.data import proc
from cc_session_control.data.project_settings import (
    SettingWriteFailure,
    SettingWriteResult,
    SettingWriteState,
)
from cc_session_control.data.rc import StartManyResult, StartResult, StartState
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
        tmux.TmuxWriteOperation.CREATE_TARGET,
        tmux.TmuxWriteStage.NEW_WINDOW,
        tmux.TmuxWriteState.SUCCEEDED,
        target=target,
    )


def test_requests_round_trip_immutable_models() -> None:
    session = _session()
    job = _job()

    session_request = tui_actions.SessionRequest.from_session(session)
    agent_request = tui_actions.AgentRequest.from_job(job)
    with pytest.raises(FrozenInstanceError):
        session.sid = "changed"
    with pytest.raises(AttributeError):
        job.respawn_flags.append("--verbose")

    assert session_request.to_session().sid == "sid-1"
    assert agent_request.to_job().respawn_flags == ("--model", "opus")


def test_stop_session_preserves_refusal_and_failure(monkeypatch) -> None:
    request = tui_actions.SessionRequest.from_session(_session())
    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda *_: tui_actions.session_ops.TakeOverOutcome(
            tui_actions.session_ops.TakeOverState.REFUSED,
        ),
    )
    refused = tui_actions.stop_session(request)
    assert refused.status is ActionStatus.REFUSED
    assert "liveness 降级" in refused.message

    monkeypatch.setattr(
        tui_actions.session_ops,
        "take_over_result",
        lambda *_: tui_actions.session_ops.TakeOverOutcome(
            tui_actions.session_ops.TakeOverState.FAILED,
        ),
    )
    failed = tui_actions.stop_session(request)
    assert failed.status is ActionStatus.FAILURE
    assert failed.message == "停止失败"


def test_stop_session_refuses_unknown_proc_probe_without_signal(monkeypatch) -> None:
    request = tui_actions.SessionRequest.from_session(_session())
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

    result = tui_actions.stop_session(request)

    assert result.status is ActionStatus.REFUSED
    assert "/proc/42/stat" in result.message
    assert "permission denied" in result.message


def test_dead_background_session_skips_liveness_and_reaches_tmux(monkeypatch) -> None:
    session = replace(_session(), alive=False, pid=None)
    request = tui_actions.SessionRequest.from_session(session)
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
        lambda tmux_session, window, cmd: (
            spawn_calls.append((tmux_session, window, cmd))
            or _created_target("project:4")
        ),
    )

    result = tui_actions.background_session(request)

    assert result.status is ActionStatus.SUCCESS
    assert result.message == "已转入后台（tmux project:4）"
    assert result.needs_refresh is True
    assert spawn_calls == [
        ("project", "sid-1", "cd /tmp/project && claude --resume sid-1")
    ]


@pytest.mark.parametrize(
    ("takeover", "expected_status", "expected_spawns"),
    [
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.REFUSED,
                "ancestor chain indeterminate",
            ),
            ActionStatus.FAILURE,
            [],
        ),
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.FAILED,
                "permission denied",
            ),
            ActionStatus.FAILURE,
            [],
        ),
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.KILLED,
            ),
            ActionStatus.SUCCESS,
            [("project", "sid-1", "cd /tmp/project && claude --resume sid-1")],
        ),
        (
            tui_actions.session_ops.TakeOverOutcome(
                tui_actions.session_ops.TakeOverState.GONE,
            ),
            ActionStatus.SUCCESS,
            [("project", "sid-1", "cd /tmp/project && claude --resume sid-1")],
        ),
    ],
)
def test_live_background_session_requires_successful_takeover_before_spawn(
    takeover: tui_actions.session_ops.TakeOverOutcome,
    expected_status: ActionStatus,
    expected_spawns: list[tuple[str, str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = tui_actions.SessionRequest.from_session(_session())
    spawn_calls: list[tuple[str, str, str]] = []
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
        tui_actions.session_ops.tmux,
        "run_in_tmux_result",
        lambda tmux_session, window, cmd: (
            spawn_calls.append((tmux_session, window, cmd))
            or _created_target("project:4")
        ),
    )

    result = tui_actions.background_session(request)

    assert result.status is expected_status
    assert spawn_calls == expected_spawns
    if takeover.success:
        assert result.message == "已转入后台（tmux project:4）"
    else:
        assert takeover.detail in result.message


def test_live_background_session_without_pid_fails_closed_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = tui_actions.SessionRequest.from_session(
        replace(_session(), pid=None),
    )
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
        tui_actions.session_ops.tmux,
        "run_in_tmux_result",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    result = tui_actions.background_session(request)

    assert result.status is ActionStatus.FAILURE
    assert "live session takeover requires a pid" in result.message


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
    assert partial_result.status is ActionStatus.PARTIAL
    assert partial_result.message.startswith("部分完成：")

    refused = CleanupExecution()
    refused.refuse(["one"], "current session cannot be determined")
    refused_result = tui_actions.run_cleanup(
        lambda _targets: refused,
        ("one",),
        "已清理 {n} 项",
    )
    assert refused_result.status is ActionStatus.REFUSED
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

    result = tui_actions.delete_session(
        tui_actions.SessionRequest.from_session(_session())
    )

    assert result.status is ActionStatus.PARTIAL
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

    assert result.status is ActionStatus.REFUSED
    assert "保护证据不完整，未删除" in result.message
    assert "session registry" in result.message
    assert "/runtime/sessions/broken.json" in result.message
    assert "invalid JSON" in result.message


def test_project_batch_result_distinguishes_partial_refused_and_failure(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tui_actions.rc,
        "start_all_listed_result",
        lambda: StartManyResult(started=2, unavailable=1, failed=1),
    )
    partial = tui_actions.start_all_projects()
    assert partial.status is ActionStatus.PARTIAL
    assert "已启动 2 个项目" in partial.message
    assert "启动失败 1 个" in partial.message

    monkeypatch.setattr(
        tui_actions.rc,
        "start_all_listed_result",
        lambda: StartManyResult(untrusted=2),
    )
    refused = tui_actions.start_all_projects()
    assert refused.status is ActionStatus.REFUSED
    assert "未信任，拒绝 2 个" in refused.message

    monkeypatch.setattr(
        tui_actions.rc,
        "start_all_listed_result",
        lambda: StartManyResult(failed=2),
    )
    assert tui_actions.start_all_projects().status is ActionStatus.FAILURE


def test_project_batch_reports_metadata_failure_target_and_detail(monkeypatch) -> None:
    failure = StartResult(
        StartState.METADATA_FAILED,
        "/project",
        "window-option: lost server connection",
        target="rc:7",
    )
    monkeypatch.setattr(
        tui_actions.rc,
        "start_all_listed_result",
        lambda: StartManyResult(failed=1, results=(failure,)),
    )

    result = tui_actions.start_all_projects()

    assert result.status is ActionStatus.FAILURE
    assert result.message == (
        "启动失败 1 个；/project 的 tmux 窗口 rc:7 已创建但元数据写入失败："
        "window-option: lost server connection"
    )


def test_start_and_setting_failures_remain_typed(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_actions.rc,
        "start_one_result",
        lambda _path: StartResult(StartState.TRUST_UNAVAILABLE, "/project"),
    )
    start = tui_actions.start_project("/project", "project")
    assert start.status is ActionStatus.REFUSED
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
    assert inventory.status is ActionStatus.REFUSED
    assert inventory.message == (
        "RC 清单不可用 — 已拒绝启动：tmux list-windows: lost server connection"
    )

    monkeypatch.setattr(
        tui_actions.rc,
        "set_rc_at_startup",
        lambda *_: SettingWriteResult(
            SettingWriteState.FAILED,
            Path("/project/.claude/settings.local.json"),
            SettingWriteFailure.WRITE,
            "read only",
        ),
    )
    setting = tui_actions.write_auto_rc("/project", "project", True)
    assert setting.status is ActionStatus.FAILURE
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

    assert result.status is ActionStatus.FAILURE
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
    assert result.status is ActionStatus.FAILURE
    assert result.message.startswith("配置写入失败（create-directory）:")
    assert list(tmp_path.iterdir()) == []


def test_expected_autostart_io_failure_becomes_typed_failure(monkeypatch) -> None:
    def fail(_path: str) -> bool:
        raise OSError("read only")

    monkeypatch.setattr(tui_actions.rc, "toggle_autostart", fail)
    result = tui_actions.toggle_autostart("/project", "project")
    assert result.status is ActionStatus.FAILURE
    assert result.message == "开机自启写入失败: read only"


def test_agent_respawn_does_not_claim_success_when_tmux_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_actions.agent_ops,
        "respawn_result",
        lambda _job: tui_actions.agent_ops.RespawnResult(
            "claude --resume resume-1 --bg",
            None,
        ),
    )

    result = tui_actions.respawn_agent(
        tui_actions.AgentRequest.from_job(_job()),
    )

    assert result.status is ActionStatus.FAILURE
    assert result.message == "重启失败：无法创建 tmux 窗口"


def test_agent_respawn_reports_typed_tmux_failure_detail(monkeypatch) -> None:
    monkeypatch.setattr(
        tui_actions.agent_ops.tmux.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["tmux", "has-session"], 5)
        ),
    )

    result = tui_actions.respawn_agent(
        tui_actions.AgentRequest.from_job(_job()),
    )

    assert result.status is ActionStatus.FAILURE
    assert result.message == ("重启失败：session-probe: tmux timed out after 5 seconds")


def test_agent_stop_preserves_all_domain_states(monkeypatch) -> None:
    request = tui_actions.AgentRequest.from_job(_job())
    cases = [
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.STOPPED,
                pid=42,
            ),
            ActionStatus.SUCCESS,
            "已发送停止信号（可能残留孤儿进程，请手动确认）",
        ),
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.NOT_RUNNING,
                detail="no live host",
            ),
            ActionStatus.REFUSED,
            "该后台 agent 未在运行",
        ),
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.REFUSED,
                detail="判活证据不完整",
            ),
            ActionStatus.REFUSED,
            "已拒绝停止：判活证据不完整",
        ),
        (
            tui_actions.agent_ops.AgentStopResult(
                tui_actions.agent_ops.AgentStopState.FAILED,
                pid=42,
                detail="permission denied",
            ),
            ActionStatus.FAILURE,
            "停止失败：permission denied",
        ),
    ]

    for domain_result, status, message in cases:
        monkeypatch.setattr(
            tui_actions.agent_ops,
            "stop_job_result",
            lambda _job, result=domain_result: result,
        )
        action = tui_actions.stop_agent(request)
        assert action.status is status
        assert action.message == message
        assert action.needs_refresh is True


def test_project_stop_preserves_all_domain_states(monkeypatch) -> None:
    cases = [
        (
            tui_actions.rc.StopResult(
                tui_actions.rc.StopState.STOPPED,
                "/project",
            ),
            ActionStatus.SUCCESS,
            "已停止 project",
        ),
        (
            tui_actions.rc.StopResult(
                tui_actions.rc.StopState.NOT_RUNNING,
                "/project",
            ),
            ActionStatus.REFUSED,
            "未在运行",
        ),
        (
            tui_actions.rc.StopResult(
                tui_actions.rc.StopState.FAILED,
                "/project",
                "lost server connection",
            ),
            ActionStatus.FAILURE,
            "停止失败：lost server connection",
        ),
    ]

    for domain_result, status, message in cases:
        monkeypatch.setattr(
            tui_actions.rc,
            "stop_one_result",
            lambda _path, result=domain_result: result,
        )
        action = tui_actions.stop_project("/project", "project")
        assert action.status is status
        assert action.message == message
        assert action.needs_refresh is True


def test_stop_all_projects_preserves_all_domain_states(monkeypatch) -> None:
    cases = [
        (
            tui_actions.rc.StopAllResult(
                tui_actions.rc.StopState.STOPPED,
                "rc",
            ),
            ActionStatus.SUCCESS,
            "已停止全部",
        ),
        (
            tui_actions.rc.StopAllResult(
                tui_actions.rc.StopState.NOT_RUNNING,
                "rc",
            ),
            ActionStatus.REFUSED,
            "本来就没在跑",
        ),
        (
            tui_actions.rc.StopAllResult(
                tui_actions.rc.StopState.FAILED,
                "rc",
                "timed out",
            ),
            ActionStatus.FAILURE,
            "停止全部失败：timed out",
        ),
    ]

    for domain_result, status, message in cases:
        monkeypatch.setattr(
            tui_actions.rc,
            "stop_all_result",
            lambda result=domain_result: result,
        )
        action = tui_actions.stop_all_projects()
        assert action.status is status
        assert action.message == message
        assert action.needs_refresh is True
