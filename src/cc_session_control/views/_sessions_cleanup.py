"""Cleanup-submenu half of the Sessions tab (mixin).

Split out of `views/sessions.py` to keep that file under the 600-line budget.
Each cleanup action is ONE `_CleanupAction` record — its counts key, R10 gate,
plan targets, preview row/文案, and executor — so `_enter_preview` and
`_confirm_cleanup` are table-driven (the same move `_keytable`/`_colspec` made
for keys and columns; the old parallel elif ladders could silently disagree).
Preview and confirm both read the view's frozen `CleanupPlan` (built with the
shared snapshot), and the `execute_*` functions revalidate each item against
fresh protection data — 删除 ⊆ 预览. The mixin reads/writes the view's own
state (`_mode`, `_plan`, `_classified`, `_cleanup_walker`, `_body`, …), so it
must be mixed into `SessionsView` only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import urwid

from ..actions import tui_actions
from ..actions.runner import Accepted
from ..data import proc
from ..data.cleanup import (
    CleanupPlan,
    execute_aged_removals,
    execute_orphan_removals,
    execute_session_removals,
    execute_zombie_removals,
)
from ..data.removal import CleanupExecution
from ..data.sessions import scan
from ..models import Session
from ._confirm import DEGRADED as _DEGRADED
from ._rows import TextRow, truncate_cells
from ._session_row import _ActionRow

if TYPE_CHECKING:
    from ..app import App


def _execute_orphans(entries: list[str]) -> CleanupExecution:
    """Orphan executor with the FULL fresh protection set: a fresh transcript
    scan feeds `known_sids`' transcript tier (the executor can't scan itself —
    data-DAG), closing the window where a sid's transcript appeared between
    preview and confirm without any registry/live trace."""
    return execute_orphan_removals(entries, sessions=scan())


def _session_line(target: object) -> str:
    if not isinstance(target, Session):
        raise TypeError("session cleanup preview requires a Session")
    when = time.strftime("%m-%d %H:%M", time.localtime(target.mtime))
    cwd = target.cwd.rstrip("/").rsplit("/", 1)[-1] if target.cwd else ""
    return f"{when}  p{target.prompts}  {truncate_cells(target.label, 60)}  ({cwd})"


@dataclass(frozen=True)
class _CleanupAction:
    """One submenu action: every fact about it lives in this record."""

    key: str
    label: str  # submenu row label
    stat: str  # `CleanupPlan.counts()` key
    gated: bool  # R10-gated (age sweep is not)
    targets: Callable[[CleanupPlan], list]  # frozen preview/exec list
    format_row: Callable[[object], str]  # one preview row per target
    execute: Callable[[list], CleanupExecution]  # revalidating executor
    none_notice: str  # "无…需要清理"
    title_tpl: str  # preview overlay title
    done_tpl: str  # post-confirm notify


# The age sweep (Strategy B) is mtime-only/session-agnostic, so it is NOT
# R10-gated; every other action is.
_CLEANUP_ACTIONS: tuple[_CleanupAction, ...] = (
    _CleanupAction(
        key="empty",
        label="空壳会话(0提问)",
        stat="empty",
        gated=True,
        targets=lambda p: p.empty,
        format_row=_session_line,
        execute=execute_session_removals,
        none_notice="无空壳会话需要清理",
        title_tpl="将清理 {n} 条空壳会话",
        done_tpl="已清理 {n} 条会话",
    ),
    _CleanupAction(
        key="short",
        label="短会话(≤2提问)",
        stat="short",
        gated=True,
        targets=lambda p: p.short,
        format_row=_session_line,
        execute=execute_session_removals,
        none_notice="无短会话(≤2提问)需要清理",
        title_tpl="将清理 {n} 条短会话(≤2提问)",
        done_tpl="已清理 {n} 条会话",
    ),
    _CleanupAction(
        key="orphans",
        label="孤儿目录(sid 键)",
        stat="orphan_dirs",
        gated=True,
        targets=lambda p: p.orphan_entries,
        format_row=str,
        execute=_execute_orphans,
        none_notice="无孤儿目录需要清理",
        title_tpl="将清理 {n} 个孤儿目录",
        done_tpl="已清理 {n} 个孤儿目录",
    ),
    _CleanupAction(
        key="zombies",
        label="僵尸会话文件(pid 键)",
        stat="zombie_procs",
        gated=True,
        targets=lambda p: p.zombie_pids,
        format_row=lambda pid: f"sessions/{pid}.json",
        execute=execute_zombie_removals,
        none_notice="无僵尸会话文件需要清理",
        title_tpl="将清理 {n} 个僵尸会话文件",
        done_tpl="已清理 {n} 个僵尸会话文件",
    ),
    _CleanupAction(
        key="aged",
        label="过期全局文件(按天)",
        stat="aged_entries",
        gated=False,
        targets=lambda p: p.aged_entries,
        format_row=str,
        execute=execute_aged_removals,
        none_notice="无过期文件需要清理",
        title_tpl="将清理 {n} 个过期项",
        done_tpl="已清理 {n} 个过期项",
    ),
)
_ACTION_BY_KEY = {a.key: a for a in _CLEANUP_ACTIONS}


class CleanupMixin:
    """Cleanup submenu + preview overlay for `SessionsView` (modes
    "cleanup"/"preview"). Key routing stays in the view's `handle_key`."""

    app: App
    _body: urwid.WidgetPlaceholder
    _classified: dict[str, int]
    _cleanup_walker: urwid.SimpleFocusListWalker
    _list_body: urwid.Widget
    _mode: str
    _plan: CleanupPlan
    _preview_action: _CleanupAction | None
    _preview_targets: list[Session | str | int]

    if TYPE_CHECKING:

        def _show_overlay(
            self,
            title: str,
            rows: list[urwid.Widget],
            height: int | None = None,
        ) -> None: ...

        def _update_footer(self) -> None: ...

    def _rebuild_cleanup(self) -> None:
        c = self._classified
        self._cleanup_walker.clear()
        for a in _CLEANUP_ACTIONS:
            self._cleanup_walker.append(_ActionRow(a.key, a.label, c.get(a.stat, 0)))

    def _enter_cleanup(self, *, show_issues: bool = True) -> None:
        self._mode = "cleanup"
        self._rebuild_cleanup()
        if show_issues and self._plan.issues:
            issue = self._plan.issues[0]
            self.app.notify(
                f"清理预览不完整（{len(self._plan.issues)} 个来源）："
                f"{issue.source}: {issue.error}"
            )
        cleanup_list = urwid.ListBox(self._cleanup_walker)
        title = urwid.AttrMap(urwid.Text(" 清理会话", align="center"), "col_header")
        box_content = urwid.Frame(cleanup_list, header=title)
        box = urwid.LineBox(box_content)
        overlay = urwid.Overlay(
            box,
            self._list_body,
            align="center",
            width=("relative", 50),
            valign="middle",
            height=min(len(self._cleanup_walker) + 4, 20),
        )
        self._body.original_widget = overlay
        self._update_footer()

    def _exit_cleanup(self) -> None:
        self._mode = "list"
        self._body.original_widget = self._list_body
        self._update_footer()

    def _selected_action(self) -> str | None:
        if not self._cleanup_walker:
            return None
        widget = self._cleanup_walker.get_focus()[0]
        if isinstance(widget, _ActionRow):
            return widget.action_key
        return None

    def _enter_preview(self, action_key: str) -> None:
        # R10/D7: session-keyed destructive sweeps need a determinable "current"
        # (without /proc every pid looks dead, so they'd nuke the live session).
        # Refuse HONESTLY — never let the refusal read as "nothing to clean".
        action = _ACTION_BY_KEY[action_key]
        if action.gated and not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return
        targets = action.targets(self._plan)
        if not targets:
            self.app.notify(action.none_notice)
            return
        self._mode = "preview"
        self._preview_action = action
        self._preview_targets = targets
        rows = [TextRow(action.format_row(t)) for t in targets]
        self._show_overlay(action.title_tpl.format(n=len(targets)), rows)
        self._update_footer()

    def _confirm_cleanup(self) -> None:
        action = self._preview_action
        if action is None:
            return
        targets = tuple(
            tui_actions.SessionRequest.from_session(target)
            if isinstance(target, Session)
            else target
            for target in self._preview_targets
        )
        outcome = self.app.submit_action(
            f"session.cleanup.{action.key}",
            lambda: tui_actions.run_cleanup(
                action.execute,
                targets,
                action.done_tpl,
            ),
        )
        if not isinstance(outcome, Accepted):
            return
        self._preview_action = None
        self._preview_targets = []
        self._enter_cleanup(show_issues=False)
