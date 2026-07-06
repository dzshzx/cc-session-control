"""Cleanup-submenu half of the Sessions tab (mixin).

Split out of `views/sessions.py` to keep that file under the 600-line budget.
Pure code move — the submenu behavior, mode names ("cleanup"/"preview"), R10
gating, and preview-first contract are unchanged. The mixin reads/writes the
view's own state (`_mode`, `_classified`, `_cleanup_walker`, `_body`, …), so it
must be mixed into `SessionsView` only.
"""

from __future__ import annotations

import time

import urwid

from ..data import proc
from ..data.cleanup import (
    list_aged_entries,
    list_orphan_dirs,
    prune_sessions,
    remove_aged_entries,
    remove_orphan_dirs,
    remove_session,
    remove_zombie_session_files,
    select_zombie_pids,
)
from ..data.sessions import scan
from ._rows import TextRow
from ._session_row import _ActionRow

# R10/D7: refusal shown when the "current" session can't be determined (no /proc)
# — session-keyed destructive ops are disabled rather than silently doing nothing.
_DEGRADED = "liveness 降级：破坏性操作已禁用"

# Submenu actions. `stat` keys index `cleanup_classified`. The age sweep
# (Strategy B) is mtime-only/session-agnostic, so it is NOT R10-gated; every
# other action is.
_CLEANUP_ACTIONS = [
    {"key": "empty",   "label": "空壳会话(0提问)",      "stat": "empty",        "gated": True},
    {"key": "short",   "label": "短会话(≤2提问)",       "stat": "short",        "gated": True},
    {"key": "orphans", "label": "孤儿目录(sid 键)",      "stat": "orphan_dirs",  "gated": True},
    {"key": "zombies", "label": "僵尸会话文件(pid 键)",  "stat": "zombie_procs", "gated": True},
    {"key": "aged",    "label": "过期全局文件(按天)",    "stat": "aged_entries", "gated": False},
]
_GATED_ACTIONS = {a["key"] for a in _CLEANUP_ACTIONS if a["gated"]}


class CleanupMixin:
    """Cleanup submenu + preview overlay for `SessionsView` (modes
    "cleanup"/"preview"). Key routing stays in the view's `handle_key`."""

    def _rebuild_cleanup(self) -> None:
        c = self._classified
        self._cleanup_walker.clear()
        for a in _CLEANUP_ACTIONS:
            count = c.get(a["stat"], 0)
            self._cleanup_walker.append(_ActionRow(a["key"], a["label"], count))

    def _enter_cleanup(self) -> None:
        self._mode = "cleanup"
        self._rebuild_cleanup()
        cleanup_list = urwid.ListBox(self._cleanup_walker)
        title = urwid.AttrMap(urwid.Text(" 清理会话", align="center"), "col_header")
        box_content = urwid.Frame(cleanup_list, header=title)
        box = urwid.LineBox(box_content)
        overlay = urwid.Overlay(
            box, self._list_body,
            align="center", width=("relative", 50),
            valign="middle", height=min(len(self._cleanup_walker) + 4, 20),
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

    def _open_preview(self, action: str, title: str, rows: list) -> None:
        """Shared preview-overlay entry for a dir/file sweep (no session list)."""
        self._mode = "preview"
        self._preview_action = action
        self._preview_sessions = []
        self._show_overlay(title, rows)
        self._update_footer()

    def _enter_preview(self, action: str) -> None:
        # R10/D7: session-keyed destructive sweeps need a determinable "current"
        # (without /proc every pid looks dead, so they'd nuke the live session).
        # Refuse HONESTLY — never let the refusal read as "nothing to clean".
        if action in _GATED_ACTIONS and not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return

        if action in ("empty", "short"):
            sessions = scan()
            if action == "empty":
                targets = prune_sessions(sessions, max_prompts=0)
                label = "空壳会话"
            else:
                targets = [s for s in prune_sessions(sessions, max_prompts=2) if s.prompts > 0]
                label = "短会话(≤2提问)"
            if not targets:
                self.app.notify(f"无{label}需要清理")
                return
            self._mode = "preview"
            self._preview_action = action
            self._preview_sessions = targets
            rows = []
            for s in targets:
                when = time.strftime("%m-%d %H:%M", time.localtime(s.mtime))
                cwd = s.cwd.rstrip("/").rsplit("/", 1)[-1] if s.cwd else ""
                line = f"{when}  p{s.prompts}  {s.label[:60]}  ({cwd})"
                rows.append(TextRow(line))
            self._show_overlay(f"将清理 {len(targets)} 条{label}", rows)
            self._update_footer()
        elif action == "orphans":
            orphan_paths = list_orphan_dirs(scan())
            if not orphan_paths:
                self.app.notify("无孤儿目录需要清理")
                return
            rows = [TextRow(p) for p in orphan_paths]
            self._open_preview(action, f"将清理 {len(orphan_paths)} 个孤儿目录", rows)
        elif action == "zombies":
            pids = select_zombie_pids(self._session_procs, self._cur)
            if not pids:
                self.app.notify("无僵尸会话文件需要清理")
                return
            rows = [TextRow(f"sessions/{pid}.json") for pid in pids]
            self._open_preview(action, f"将清理 {len(pids)} 个僵尸会话文件", rows)
        elif action == "aged":
            entries = list_aged_entries()
            if not entries:
                self.app.notify("无过期文件需要清理")
                return
            rows = [TextRow(e) for e in entries]
            self._open_preview(action, f"将清理 {len(entries)} 个过期项", rows)

    def _confirm_cleanup(self) -> None:
        action = self._preview_action
        if action in ("empty", "short"):
            removed = sum(1 for t in self._preview_sessions if remove_session(t))
            self.app.notify(f"已清理 {removed} 条会话")
        elif action == "orphans":
            count = remove_orphan_dirs(scan())
            self.app.notify(f"已清理 {count} 个孤儿目录")
        elif action == "zombies":
            count = remove_zombie_session_files(self._session_procs, self._cur)
            self.app.notify(f"已清理 {count} 个僵尸会话文件")
        elif action == "aged":
            count = remove_aged_entries()
            self.app.notify(f"已清理 {count} 个过期项")
        self._preview_action = None
        self._preview_sessions = []
        self._enter_cleanup()
        self.app.trigger_async_refresh()
