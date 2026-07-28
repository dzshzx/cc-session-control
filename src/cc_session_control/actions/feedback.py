"""Operator-facing summaries for typed cleanup execution results."""

from __future__ import annotations

from ..data.removal import CleanupExecution


def _details(result: CleanupExecution) -> list[str]:
    details: list[str] = []
    if result.failed and result.removed:
        details.append(f"已删除路径 {len(result.removed)}")
    if result.failed:
        first = result.failed[0]
        detail = f"失败 {len(result.failed)}"
        if first.error:
            detail += f"（{first.path}: {first.error}）"
        details.append(detail)
    if result.skipped:
        details.append(f"跳过 {len(result.skipped)}（{result.skipped[0].reason}）")
    if result.refused:
        details.append(f"拒绝 {len(result.refused)}（{result.refused[0].reason}）")
    if result.missing_targets:
        details.append(f"已不存在 {len(result.missing_targets)}")
    return details


def format_cleanup_notice(result: CleanupExecution, done_template: str) -> str:
    """Summarize multi-target cleanup without turning partial work into success."""
    completed = len(result.completed)
    details = _details(result)
    if completed and not details:
        return done_template.format(n=completed)
    if completed:
        return f"部分完成：{done_template.format(n=completed)}；" + "；".join(details)
    if result.removed:
        return "清理部分失败：" + "；".join(details)
    if result.refused:
        return "已拒绝清理：" + "；".join(details)
    if result.failed:
        return "清理失败：" + "；".join(details)
    if details:
        return "未清理任何项目：" + "；".join(details)
    return "无可清理内容"


def format_delete_notice(result: CleanupExecution) -> str:
    """Summarize deletion of one session or background-agent artifact set."""
    if result.completed and not result.failed:
        return "已删除"
    if result.refused:
        return f"拒绝删除：{result.refused[0].reason}"
    if result.skipped:
        return f"未删除：{result.skipped[0].reason}"
    if result.failed:
        first = result.failed[0]
        suffix = f"：{first.error}" if first.error else ""
        if result.removed:
            return (
                f"删除部分失败：已删除路径 {len(result.removed)}；"
                f"失败 {len(result.failed)}{suffix}"
            )
        return f"删除失败{suffix}"
    return "无可删除内容"
