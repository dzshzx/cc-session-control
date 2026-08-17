"""Typed adapters for mutations that return to the TUI.

Views pass the frozen ``Session`` domain models directly to
these action adapters. Workers receive only those frozen models and return
``ActionResult`` data; they never receive or mutate an urwid widget, walker,
selection, or App.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import cfg
from ..data import cleanup, providers
from ..data.curation import CurationWriteState, set_hidden, set_pinned
from ..data.removal import CleanupExecution
from ..models import Session
from . import session_ops
from .feedback import (
    format_cleanup_notice,
    format_cli_delete_notice,
    format_delete_notice,
)
from .runner import ActionResult

type CleanupTarget = Session | str | int
type CleanupExecutor = Callable[[list], CleanupExecution]


def _hosted_refusal(session: Session) -> ActionResult | None:
    if session.hosted:
        return ActionResult("托管会话只读，csctl 不接回、停止、分叉或删除")
    return None


def stop_session(session: Session) -> ActionResult:
    if refusal := _hosted_refusal(session):
        return refusal
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
    if refusal := _hosted_refusal(session):
        return refusal
    outcome = session_ops.do_tmux_resume_result(session)
    if outcome.target is None:
        detail = f"：{outcome.detail}" if outcome.detail else ""
        return ActionResult(f"转入后台失败{detail}", needs_refresh=True)
    return ActionResult(
        f"已转入后台（tmux {outcome.target}）",
        needs_refresh=True,
    )


def delete_session(session: Session) -> ActionResult:
    if refusal := _hosted_refusal(session):
        return refusal
    if providers.get(session.provider).caps.cleanup:
        return _delete_result(cleanup.remove_session(session))
    # Delegated official-CLI delete (`codex delete <sid>`): a bypass BESIDE
    # the cleanup data boundary, not a relaxation of it — csctl's own removal
    # seam keeps refusing non-Claude state (`cleanup.remove_session`), and
    # deletion authority stays with the owning CLI. Loud (TypeError) for a
    # provider without delete verbs — the view gate keeps those out.
    result = providers.execute_cli_delete(session.provider, session.sid)
    return ActionResult(format_cli_delete_notice(result), needs_refresh=True)


def copy_resume_command(session: Session) -> ActionResult:
    if refusal := _hosted_refusal(session):
        return refusal
    if session.provider.split(":", 1)[0] == "codex":
        resolution = session_ops.session_for_execution(session, fork=False)
        if not resolution.success or resolution.session is None:
            detail = resolution.detail or "执行时会话证据不完整"
            return ActionResult(f"复制失败：{detail}", needs_refresh=True)
        session = resolution.session
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


def toggle_project_pin(path: str, name: str, pinned: bool) -> ActionResult:
    """Pin/unpin one directory in the curation store (ADR-0007)."""
    result = set_pinned(cfg.curation_file, path, pinned)
    if result.state is CurationWriteState.FAILED:
        reason = result.failure.value if result.failure is not None else "unknown"
        return ActionResult(f"钉选写入失败（{reason}）: {result.detail}")
    verb = "已钉选" if pinned else "已取消钉选"
    suffix = "（无变化）" if result.state is CurationWriteState.UNCHANGED else ""
    return ActionResult(f"{verb} {name}{suffix}", needs_refresh=True)


def toggle_project_hidden(path: str, name: str, hidden: bool) -> ActionResult:
    """Hide/unhide one directory in the curation store (ADR-0007)."""
    result = set_hidden(cfg.curation_file, path, hidden)
    if result.state is CurationWriteState.FAILED:
        reason = result.failure.value if result.failure is not None else "unknown"
        return ActionResult(f"隐藏写入失败（{reason}）: {result.detail}")
    verb = "已隐藏" if hidden else "已取消隐藏"
    suffix = "（无变化）" if result.state is CurationWriteState.UNCHANGED else ""
    return ActionResult(f"{verb} {name}{suffix}", needs_refresh=True)


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
