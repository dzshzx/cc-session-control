"""Typed adapters for mutations that return to the TUI.

Views project immutable published row models into action-specific frozen
requests before submission. Workers receive only those requests and return
``ActionResult`` data; they never receive or mutate an urwid widget, walker,
selection, or App.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..data import cleanup, rc
from ..data.project_settings import SettingWriteState
from ..data.removal import CleanupExecution
from ..models import AgentJob, Session
from . import agent_ops, session_ops
from .feedback import format_cleanup_notice, format_delete_notice
from .runner import ActionResult


@dataclass(frozen=True)
class SessionRequest:
    sid: str
    cwd: str
    label: str
    mtime: float
    prompts: int
    pid: int | None
    alive: bool
    current: bool
    proc_start: str
    file: str
    source: str
    agent_short: str | None
    tmux_target: str | None

    @classmethod
    def from_session(cls, session: Session) -> SessionRequest:
        return cls(
            sid=session.sid,
            cwd=session.cwd,
            label=session.label,
            mtime=session.mtime,
            prompts=session.prompts,
            pid=session.pid,
            alive=session.alive,
            current=session.current,
            proc_start=session.proc_start,
            file=session.file,
            source=session.source,
            agent_short=session.agent_short,
            tmux_target=session.tmux_target,
        )

    def to_session(self) -> Session:
        return Session(
            sid=self.sid,
            cwd=self.cwd,
            label=self.label,
            mtime=self.mtime,
            prompts=self.prompts,
            pid=self.pid,
            alive=self.alive,
            current=self.current,
            proc_start=self.proc_start,
            file=self.file,
            source=self.source,
            agent_short=self.agent_short,
            tmux_target=self.tmux_target,
        )


@dataclass(frozen=True)
class AgentRequest:
    short: str
    sid: str
    resume_sid: str
    state: str
    tempo: str
    cwd: str
    name: str
    env_suffix: str
    respawn_flags: tuple[str, ...]
    host_pid: int | None
    host_alive: bool

    @classmethod
    def from_job(cls, job: AgentJob) -> AgentRequest:
        return cls(
            short=job.short,
            sid=job.sid,
            resume_sid=job.resume_sid,
            state=job.state,
            tempo=job.tempo,
            cwd=job.cwd,
            name=job.name,
            env_suffix=job.env_suffix,
            respawn_flags=tuple(job.respawn_flags),
            host_pid=job.host_pid,
            host_alive=job.host_alive,
        )

    def to_job(self) -> AgentJob:
        return AgentJob(
            short=self.short,
            sid=self.sid,
            resume_sid=self.resume_sid,
            state=self.state,
            tempo=self.tempo,
            cwd=self.cwd,
            name=self.name,
            env_suffix=self.env_suffix,
            respawn_flags=self.respawn_flags,
            host_pid=self.host_pid,
            host_alive=self.host_alive,
        )


type CleanupTarget = SessionRequest | str | int
type CleanupExecutor = Callable[[list], CleanupExecution]


def stop_session(request: SessionRequest) -> ActionResult:
    if request.pid is None:
        return ActionResult.failure("停止失败", needs_refresh=True)
    outcome = session_ops.take_over(request.pid, request.proc_start)
    if outcome in session_ops.TAKE_OVER_OK:
        return ActionResult.success("已停止", needs_refresh=True)
    if outcome == "refused":
        return ActionResult.refused(
            "liveness 降级：破坏性操作已禁用",
            needs_refresh=True,
        )
    return ActionResult.failure("停止失败", needs_refresh=True)


def background_session(request: SessionRequest) -> ActionResult:
    target = session_ops.do_tmux_resume(request.to_session())
    if target is None:
        return ActionResult.failure("转入后台失败", needs_refresh=True)
    return ActionResult.success(
        f"已转入后台（tmux {target}）",
        needs_refresh=True,
    )


def delete_session(request: SessionRequest) -> ActionResult:
    result = cleanup.remove_session(request.to_session())
    return _delete_result(result)


def copy_resume_command(request: SessionRequest) -> ActionResult:
    command = session_ops.resume_cmd(request.to_session())
    if session_ops.to_clipboard(command):
        return ActionResult.success("已复制")
    return ActionResult.failure(f"复制失败: {command}")


def respawn_agent(request: AgentRequest) -> ActionResult:
    result = agent_ops.respawn_result(request.to_job())
    if not result.success:
        return ActionResult.failure(
            "重启失败：无法创建 tmux 窗口",
            needs_refresh=True,
        )
    return ActionResult.success(
        f"已重启：{result.command}",
        needs_refresh=True,
    )


def remove_agent(request: AgentRequest) -> ActionResult:
    return _delete_result(agent_ops.remove_job(request.to_job()))


def stop_agent(request: AgentRequest) -> ActionResult:
    result = agent_ops.stop_job_result(request.to_job())
    if result.state is agent_ops.AgentStopState.STOPPED:
        return ActionResult.success(
            "已发送停止信号（可能残留孤儿进程，请手动确认）",
            needs_refresh=True,
        )
    if result.state is agent_ops.AgentStopState.NOT_RUNNING:
        return ActionResult.refused(
            "该后台 agent 未在运行",
            needs_refresh=True,
        )
    if result.state is agent_ops.AgentStopState.REFUSED:
        detail = result.detail or "安全判定不可用"
        return ActionResult.refused(
            f"已拒绝停止：{detail}",
            needs_refresh=True,
        )
    detail = result.detail or "无法发送停止信号"
    return ActionResult.failure(
        f"停止失败：{detail}",
        needs_refresh=True,
    )


def start_project(path: str, name: str) -> ActionResult:
    result = rc.start_one_result(path)
    if result.state is rc.StartState.STARTED:
        return ActionResult.success(f"已启动 {name}", needs_refresh=True)
    if result.state is rc.StartState.TRUST_UNAVAILABLE:
        message = "项目设置不可用 — 已拒绝启动"
        return ActionResult.refused(message, needs_refresh=True)
    if result.state is rc.StartState.UNTRUSTED:
        return ActionResult.refused("未信任 — 已拒绝启动", needs_refresh=True)
    if result.state is rc.StartState.ALREADY_RUNNING:
        return ActionResult.refused("已在运行", needs_refresh=True)
    if result.state is rc.StartState.NOT_DIRECTORY:
        return ActionResult.refused(
            "目录缺失 — 无法启动（可用 a 键移出自启列表）",
            needs_refresh=True,
        )
    return ActionResult.failure("启动失败", needs_refresh=True)


def stop_project(path: str, name: str) -> ActionResult:
    result = rc.stop_one_result(path)
    if result.state is rc.StopState.STOPPED:
        return ActionResult.success(f"已停止 {name}", needs_refresh=True)
    if result.state is rc.StopState.NOT_RUNNING:
        return ActionResult.refused("未在运行", needs_refresh=True)
    detail = result.detail or "tmux 操作失败"
    return ActionResult.failure(f"停止失败：{detail}", needs_refresh=True)


def toggle_autostart(path: str, name: str) -> ActionResult:
    try:
        enabled = rc.toggle_autostart(path)
    except (OSError, UnicodeError) as exc:
        return ActionResult.failure(f"开机自启写入失败: {exc}")
    state = "开" if enabled else "关"
    return ActionResult.success(
        f"{name} 开机自启: {state}",
        needs_refresh=True,
    )


def write_auto_rc(
    path: str,
    name: str,
    value: bool | None,
) -> ActionResult:
    result = rc.set_rc_at_startup(path, value)
    if result.state is SettingWriteState.FAILED:
        reason = result.failure.value if result.failure is not None else "unknown"
        return ActionResult.failure(
            f"配置写入失败（{reason}）: {result.detail}",
        )
    shown = {True: "开", False: "关", None: "未设置"}[value]
    suffix = "（无变化）" if result.state is SettingWriteState.UNCHANGED else ""
    return ActionResult.success(
        f"{name} 自动远控: {shown}{suffix}",
        needs_refresh=True,
    )


def start_all_projects() -> ActionResult:
    try:
        result = rc.start_all_listed_result()
    except (OSError, UnicodeError) as exc:
        return ActionResult.failure(f"启动列表读取失败: {exc}")
    parts = [f"已启动 {result.started} 个项目"]
    if result.unavailable:
        parts.append(f"项目设置不可用，拒绝 {result.unavailable} 个")
    if result.untrusted:
        parts.append(f"未信任，拒绝 {result.untrusted} 个")
    if result.failed:
        parts.append(f"启动失败 {result.failed} 个")
    message = "；".join(parts)
    if result.started and (result.unavailable or result.untrusted or result.failed):
        return ActionResult.partial(message, needs_refresh=True)
    if result.started:
        return ActionResult.success(message, needs_refresh=True)
    if result.failed:
        return ActionResult.failure(message, needs_refresh=True)
    if result.unavailable or result.untrusted:
        return ActionResult.refused(message, needs_refresh=True)
    return ActionResult.success(message, needs_refresh=True)


def stop_all_projects() -> ActionResult:
    result = rc.stop_all_result()
    if result.state is rc.StopState.STOPPED:
        return ActionResult.success("已停止全部", needs_refresh=True)
    if result.state is rc.StopState.NOT_RUNNING:
        return ActionResult.refused("本来就没在跑", needs_refresh=True)
    detail = result.detail or "tmux 操作失败"
    return ActionResult.failure(f"停止全部失败：{detail}", needs_refresh=True)


def run_cleanup(
    execute: CleanupExecutor,
    targets: tuple[CleanupTarget, ...],
    done_template: str,
) -> ActionResult:
    mutable_targets = [
        target.to_session() if isinstance(target, SessionRequest) else target
        for target in targets
    ]
    result = execute(mutable_targets)
    message = format_cleanup_notice(result, done_template)
    if result.completed and not result.incomplete:
        return ActionResult.success(message, needs_refresh=True)
    if result.completed or (result.removed and result.incomplete):
        return ActionResult.partial(message, needs_refresh=True)
    if result.refused or result.skipped:
        return ActionResult.refused(message, needs_refresh=True)
    if result.failed:
        return ActionResult.failure(message, needs_refresh=True)
    return ActionResult.success(message, needs_refresh=True)


def _delete_result(result: CleanupExecution) -> ActionResult:
    message = format_delete_notice(result)
    if result.completed and not result.incomplete:
        return ActionResult.success(message, needs_refresh=True)
    if result.removed and result.incomplete:
        return ActionResult.partial(message, needs_refresh=True)
    if result.refused or result.skipped:
        return ActionResult.refused(message, needs_refresh=True)
    if result.failed:
        return ActionResult.failure(message, needs_refresh=True)
    return ActionResult.success(message, needs_refresh=True)
