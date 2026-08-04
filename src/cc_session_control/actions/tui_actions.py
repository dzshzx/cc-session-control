"""Typed adapters for mutations that return to the TUI.

Views pass the frozen ``Session``/``AgentJob`` domain models directly to
these action adapters. Workers receive only those frozen models and return
``ActionResult`` data; they never receive or mutate an urwid widget, walker,
selection, or App.
"""

from __future__ import annotations

from collections.abc import Callable

from ..data import cleanup, rc
from ..data.project_settings import SettingWriteState, write_rc_at_startup
from ..data.removal import CleanupExecution
from ..models import AgentJob, Session
from . import agent_ops, session_ops
from .feedback import format_cleanup_notice, format_delete_notice
from .runner import ActionResult

type CleanupTarget = Session | str | int
type CleanupExecutor = Callable[[list], CleanupExecution]


def stop_session(session: Session) -> ActionResult:
    if session.pid is None:
        return ActionResult("停止失败", needs_refresh=True)
    outcome = session_ops.take_over_result(session.pid, session.proc_start)
    if outcome.success:
        return ActionResult("已停止", needs_refresh=True)
    if outcome.state is session_ops.TakeOverState.REFUSED:
        detail = f"：{outcome.detail}" if outcome.detail else ""
        return ActionResult(
            f"liveness 降级：破坏性操作已禁用{detail}",
            needs_refresh=True,
        )
    detail = f"：{outcome.detail}" if outcome.detail else ""
    return ActionResult(f"停止失败{detail}", needs_refresh=True)


def background_session(session: Session) -> ActionResult:
    outcome = session_ops.do_tmux_resume_result(session)
    if outcome.target is None:
        detail = f"：{outcome.detail}" if outcome.detail else ""
        return ActionResult(f"转入后台失败{detail}", needs_refresh=True)
    return ActionResult(
        f"已转入后台（tmux {outcome.target}）",
        needs_refresh=True,
    )


def delete_session(session: Session) -> ActionResult:
    result = cleanup.remove_session(session)
    return _delete_result(result)


def copy_resume_command(session: Session) -> ActionResult:
    command = session_ops.resume_cmd(session)
    if not session_ops.to_clipboard(command):
        return ActionResult(f"复制失败: {command}")
    if session.archived:
        # The copied payload IS the provider's official un-archive command
        # (resume_cmd's archived branch) — say so, never imply a resume.
        return ActionResult("已复制 unarchive 命令（会话已归档）")
    if session.provider != "claude" and session.alive:
        # Non-Claude live rows copy a direct provider resume command
        # (ADR-0005) — it never stops the running process, unlike the
        # Claude-only guarded takeover the plain notice implies elsewhere.
        return ActionResult("已复制（直接 resume 命令，不会终止原进程）")
    return ActionResult("已复制")


def respawn_agent(job: AgentJob) -> ActionResult:
    result = agent_ops.respawn_result(job)
    if not result.success:
        detail = result.detail or "无法创建 tmux 窗口"
        return ActionResult(
            f"重启失败：{detail}",
            needs_refresh=True,
        )
    return ActionResult(
        f"已重启：{result.command}",
        needs_refresh=True,
    )


def remove_agent(job: AgentJob) -> ActionResult:
    return _delete_result(agent_ops.remove_job(job))


def stop_agent(job: AgentJob) -> ActionResult:
    result = agent_ops.stop_job_result(job)
    if result.state is agent_ops.AgentStopState.STOPPED:
        return ActionResult(
            "已发送停止信号（可能残留孤儿进程，请手动确认）",
            needs_refresh=True,
        )
    if result.state is agent_ops.AgentStopState.NOT_RUNNING:
        return ActionResult(
            "该后台 agent 未在运行",
            needs_refresh=True,
        )
    if result.state is agent_ops.AgentStopState.REFUSED:
        detail = result.detail or "安全判定不可用"
        return ActionResult(
            f"已拒绝停止：{detail}",
            needs_refresh=True,
        )
    detail = result.detail or "无法发送停止信号"
    return ActionResult(
        f"停止失败：{detail}",
        needs_refresh=True,
    )


def start_project(path: str, name: str) -> ActionResult:
    result = rc.start_one_result(path)
    if result.state is rc.StartState.STARTED:
        return ActionResult(f"已启动 {name}", needs_refresh=True)
    if result.state is rc.StartState.TRUST_UNAVAILABLE:
        message = "项目设置不可用 — 已拒绝启动"
        return ActionResult(message, needs_refresh=True)
    if result.state is rc.StartState.UNTRUSTED:
        return ActionResult("未信任 — 已拒绝启动", needs_refresh=True)
    if result.state is rc.StartState.INVENTORY_UNAVAILABLE:
        detail = f"：{result.detail}" if result.detail else ""
        return ActionResult(
            f"RC 清单不可用 — 已拒绝启动{detail}",
            needs_refresh=True,
        )
    if result.state is rc.StartState.ALREADY_RUNNING:
        return ActionResult("已在运行", needs_refresh=True)
    if result.state is rc.StartState.NOT_DIRECTORY:
        return ActionResult(
            "目录缺失 — 无法启动",
            needs_refresh=True,
        )
    if result.state is rc.StartState.METADATA_FAILED:
        target = result.target or "unknown"
        detail = result.detail or "tmux 元数据写入失败"
        return ActionResult(
            f"启动不完整：tmux 窗口 {target} 已创建，但元数据写入失败：{detail}",
            needs_refresh=True,
        )
    detail = f"：{result.detail}" if result.detail else ""
    return ActionResult(f"启动失败{detail}", needs_refresh=True)


def stop_project(path: str, name: str) -> ActionResult:
    result = rc.stop_one_result(path)
    if result.state is rc.StopState.STOPPED:
        return ActionResult(f"已停止 {name}", needs_refresh=True)
    if result.state is rc.StopState.NOT_RUNNING:
        return ActionResult("未在运行", needs_refresh=True)
    detail = result.detail or "tmux 操作失败"
    return ActionResult(f"停止失败：{detail}", needs_refresh=True)


def write_auto_rc(
    path: str,
    name: str,
    value: bool | None,
) -> ActionResult:
    result = write_rc_at_startup(path, value)
    if result.state is SettingWriteState.FAILED:
        reason = result.failure.value if result.failure is not None else "unknown"
        return ActionResult(
            f"配置写入失败（{reason}）: {result.detail}",
        )
    shown = {True: "开", False: "关", None: "未设置"}[value]
    suffix = "（无变化）" if result.state is SettingWriteState.UNCHANGED else ""
    return ActionResult(
        f"{name} 自动远控: {shown}{suffix}",
        needs_refresh=True,
    )


def stop_all_projects() -> ActionResult:
    result = rc.stop_all_result()
    if result.state is rc.StopState.STOPPED:
        return ActionResult("已停止全部", needs_refresh=True)
    if result.state is rc.StopState.NOT_RUNNING:
        return ActionResult("本来就没在跑", needs_refresh=True)
    detail = result.detail or "tmux 操作失败"
    return ActionResult(f"停止全部失败：{detail}", needs_refresh=True)


def run_cleanup(
    execute: CleanupExecutor,
    targets: tuple[CleanupTarget, ...],
    done_template: str,
) -> ActionResult:
    result = execute(list(targets))
    return ActionResult(
        format_cleanup_notice(result, done_template), needs_refresh=True
    )


def _delete_result(result: CleanupExecution) -> ActionResult:
    return ActionResult(format_delete_notice(result), needs_refresh=True)
