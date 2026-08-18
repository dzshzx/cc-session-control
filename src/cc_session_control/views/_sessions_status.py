"""Sessions status-line composition — pure text, no widget state."""

from __future__ import annotations

from ..data import providers
from ..models import InventoryIssue, Session, issue_detail


def status_text(
    *,
    all_sessions: list[Session],
    shown_count: int,
    filter_text: str,
    show_hidden: bool,
    show_archived: bool,
    provider_issues: tuple[InventoryIssue, ...],
    cleanup_stats: dict[str, int],
) -> str:
    alive_n = sum(1 for s in all_sessions if s.alive)
    provider_text = ""
    by_provider: dict[str, int] = {}
    for s in all_sessions:
        by_provider[s.provider] = by_provider.get(s.provider, 0) + 1
    if len(by_provider) > 1:
        provider_text = " · " + " ".join(
            f"{providers.get(key).label} {count}"
            for key, count in sorted(by_provider.items())
        )
    degraded_text = ""
    if provider_issues:
        degraded_text = (
            f" · 外部源降级 {len(provider_issues)}"
            f"（{issue_detail(provider_issues[:1])}）"
        )
    flt = f" · 过滤「{filter_text}」" if filter_text else ""
    empty = cleanup_stats.get("empty", 0)
    short = cleanup_stats.get("short", 0)
    orphans = cleanup_stats.get("orphans", 0)
    cleanup_text = ""
    hidden_n = sum(1 for s in all_sessions if s.bridge_or_sdk)
    hidden_text = ""
    archived_n = sum(1 for s in all_sessions if s.archived)
    archived_text = ""
    if archived_n:
        archived_text = (
            f" · 归档 {archived_n}" if show_archived else f" · 归档已隐藏 {archived_n}"
        )
    tmux_unknown = [
        s
        for s in all_sessions
        if s.alive and not s.tmux_target and not s.tmux_inventory_complete
    ]
    tmux_text = ""
    if tmux_unknown:
        detail = next(
            (s.tmux_inventory_detail for s in tmux_unknown if s.tmux_inventory_detail),
            "",
        )
        tmux_text = f" · tmux 驻留未知 {len(tmux_unknown)}"
        if detail:
            tmux_text += f"（{detail}）"
    if hidden_n:
        hidden_text = (
            f" · 桥接/SDK {hidden_n}"
            if show_hidden
            else f" · 桥接/SDK已隐藏 {hidden_n}"
        )
    if empty or short or orphans:
        parts = []
        if empty:
            parts.append(f"空壳 {empty}")
        if short:
            parts.append(f"短 {short}")
        if orphans:
            parts.append(f"孤儿 {orphans}")
        cleanup_text = f" · {' · '.join(parts)}"
    return (
        f" 共 {len(all_sessions)} 条会话{provider_text} · 运行 {alive_n}"
        f" · 显示 {shown_count}"
        f"{flt}{hidden_text}{archived_text}{tmux_text}{degraded_text}{cleanup_text}"
    )
