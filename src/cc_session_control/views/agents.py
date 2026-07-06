"""Agents view — the 后台 (background agents) tab.

Lists `jobs/<short>/state.json` records (registry.AgentJob, enriched with host
liveness) and wires their lifecycle to `actions/agent_ops`: respawn, takeover
(via the existing resume path), read-only watch, settled-only remove, and
live-only stop. Satisfies the TabView Protocol structurally so `app.py` drives
it generically. All user-facing strings are Simplified Chinese; the orphan-risk
warning surfaced on `stop` is a capability red line (R4.5 / AC4).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import urwid

from ..actions import agent_ops
from ..data import proc, registry
from ..models import AgentJob
from ._colspec import header_columns, row_columns
from ._confirm import DEGRADED as _DEGRADED
from ._confirm import confirm_takeover, stop_message
from ._rows import TextRow

if TYPE_CHECKING:
    from ..data.snapshot import WorldSnapshot

    from ..app import App


# One spec drives header + rows (_colspec.py). The 状态 text column already
# carries the state in words, so the mark column only needs the ●/○ shape —
# state never rides on color alone here either.
_AGENT_COLS = [
    (2, "left", ""),
    (("weight", 2), "left", "名称"),
    (8, "left", "状态"),
    (8, "left", "节奏"),
    (("weight", 2), "left", "目录"),
    (("weight", 2), "left", "环境后缀"),
]

_AGENTS_HEADER = header_columns(_AGENT_COLS)


class AgentRow(urwid.WidgetWrap):
    def __init__(self, job: AgentJob) -> None:
        self.job = job
        mark = "●" if job.host_alive else "○"
        cwd = job.cwd.rstrip("/").rsplit("/", 1)[-1] if job.cwd else ""
        cols = row_columns(_AGENT_COLS, [
            mark,
            job.name or job.short,
            job.state or "-",
            job.tempo or "-",
            cwd,
            job.env_suffix or "-",
        ])
        attr = "alive" if job.host_alive else "dead"
        mapped = urwid.AttrMap(cols, attr, focus_map={"alive": "selected", "dead": "selected", None: "selected"})
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class AgentsView:
    # mode: "list" | "help" | "watch"
    def __init__(self, app: App) -> None:
        self.app = app
        self._jobs: list[AgentJob] = []
        self._pending: list[AgentJob] | None = None
        self._loaded = False
        self._mode = "list"

        self.status = urwid.AttrMap(urwid.Text(" 扫描中…"), "status")
        col_header = urwid.AttrMap(_AGENTS_HEADER, "col_header")
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)
        self._list_body = urwid.AttrMap(self.listbox, {None: "body"})
        self._body = urwid.WidgetPlaceholder(self._list_body)
        self.widget = urwid.Frame(self._body, header=col_header, footer=self.status)

    # --- TabView contract ---

    def keyhints(self) -> str:
        if self._mode in ("help", "watch"):
            # "其余" is honest: the prefix's Tab/q stay global (Tab switches
            # tabs, q QUITS — neither returns to the list).
            return "其余任意键返回"
        return f"{agent_ops.KEYHINTS} · ? 详细说明"

    def deactivate(self) -> None:
        """TabView hook: called on tab switch-away. No transient footer modes
        here — help/watch live in the body widget and stay visibly modal."""

    def _enrich(self, jobs: list[AgentJob]) -> list[AgentJob]:
        """Fill host liveness for the self-fetch path (snapshot already enriched).

        Returns fresh copies via `dataclasses.replace` (like `snapshot._enrich_jobs`)
        so the registry's ~5s-TTL cached AgentJob objects are never mutated.
        """
        out: list[AgentJob] = []
        for job in jobs:
            pid, alive = agent_ops.job_host(job)
            out.append(replace(job, host_pid=pid, host_alive=alive))
        return out

    def load(self) -> None:
        self._jobs = self._enrich(registry.read_agent_jobs())
        self._loaded = True
        self._rebuild()

    def fetch_pending(self, snapshot: WorldSnapshot | None = None) -> None:
        """Worker-thread data fetch. Only sets pending fields — no widgets."""
        if snapshot is not None:
            self._pending = snapshot.agent_jobs
        else:
            self._pending = self._enrich(registry.read_agent_jobs())

    def apply_data(self) -> None:
        if self._pending is not None:
            self._jobs = self._pending
            self._pending = None
            self._loaded = True
            if self._mode == "list":
                self._rebuild()

    # --- rendering ---

    def _rebuild(self) -> None:
        focus_pos = self.walker.get_focus()[1] if self.walker else 0
        self.walker.clear()
        for job in self._jobs:
            self.walker.append(AgentRow(job))
        if not self._jobs:
            self.walker.append(urwid.AttrMap(urwid.Text(" 暂无后台 agent"), "dead"))
        if self.walker and focus_pos is not None:
            self.walker.set_focus(min(focus_pos, len(self.walker) - 1))
        alive_n = sum(1 for j in self._jobs if j.host_alive)
        self.status.original_widget.set_text(
            f" 共 {len(self._jobs)} 个后台 agent · 运行 {alive_n}"
        )

    def _selected(self) -> AgentJob | None:
        if not self.walker:
            return None
        widget = self.walker.get_focus()[0]
        if isinstance(widget, AgentRow):
            return widget.job
        return None

    def _update_footer(self) -> None:
        if self.app.views[self.app._active] is not self:
            return
        self.app.set_hints(self.keyhints())

    def _show_overlay(self, title: str, rows: list, height: int | None = None) -> None:
        walker = urwid.SimpleFocusListWalker(rows)
        listbox = urwid.ListBox(walker)
        header = urwid.AttrMap(urwid.Text(f" {title}", align="center"), "col_header")
        box = urwid.LineBox(urwid.Frame(listbox, header=header))
        h = height or min(len(rows) + 4, 30)
        overlay = urwid.Overlay(
            box, self._list_body,
            align="center", width=("relative", 80),
            valign="middle", height=h,
        )
        self._body.original_widget = overlay

    def _exit_overlay(self) -> None:
        self._mode = "list"
        self._body.original_widget = self._list_body
        self._rebuild()
        self._update_footer()

    # --- key dispatch ---

    def handle_key(self, key: str) -> None:
        if self._mode in ("help", "watch"):
            # `r` keeps its footer-prefix meaning (刷新) even here, so the
            # "其余任意键返回" hint stays exact.
            if key == "r":
                self.app.refresh_with_notice()
                return
            self._exit_overlay()
            return

        job = self._selected()

        if key == "r":
            # Unified verb table: `r` is refresh on EVERY tab (respawn moved to R).
            self.app.refresh_with_notice()
        elif key == "R" and job:
            cmd = agent_ops.respawn(job)
            self.app.notify(f"已重启：{cmd}")
            self.app.trigger_async_refresh()
        elif key in ("enter", "o") and job:
            self._takeover(job)
        elif key == "w" and job:
            self._watch(job)
        elif key == "d" and job:
            self._remove(job)
        elif key == "s" and job:
            self._stop(job)
        elif key == "?":
            self._show_help()

    def _takeover(self, job: AgentJob) -> None:
        s = agent_ops.resume_takeover(job)
        if s.current:
            self.app.notify("不能接回当前会话")
            return
        # B1: takeover of a RUNNING worker kills its host pid (should_kill) — same
        # as Sessions Enter-live. A dead worker resumes directly, unconfirmed.
        confirm_takeover(
            self.app, s, "接回后台 agent",
            lambda: self.app.exit_with_resume(s, fork=False),
            name=job.name or job.short,
        )

    def _watch(self, job: AgentJob) -> None:
        path = agent_ops.watch(job)
        if not path:
            self.app.notify("无 timeline 可查看")
            return
        lines: list[str] = []
        try:
            with open(path, errors="ignore") as fh:
                lines = fh.read().splitlines()[-200:]
        except Exception:
            self.app.notify("读取 timeline 失败")
            return
        rows = [TextRow(line) for line in lines] or [TextRow("(空)")]
        self._mode = "watch"
        self._show_overlay(f"timeline（只读）· {job.name or job.short}", rows)
        self._update_footer()

    def _remove(self, job: AgentJob) -> None:
        if job.host_alive:
            self.app.notify("运行中的后台 agent 不能删除，先停止")
            return
        if not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return
        ok = agent_ops.remove_job(job)
        self.app.notify("已删除" if ok else "删除失败")
        self.app.trigger_async_refresh()

    def _stop(self, job: AgentJob) -> None:
        # Degrade gate FIRST (R2): off /proc we can't prove this isn't csctl's own
        # session — refuse honestly before confirming.
        if not proc.current_determinable():
            self.app.notify(_DEGRADED)
            return
        if not job.host_alive:
            self.app.notify("后台 agent 未在运行")
            return
        self.app.confirm(
            stop_message("停止后台 agent", job.name or job.short),
            lambda: self._do_stop(job),
        )

    def _do_stop(self, job: AgentJob) -> None:
        """Stop body, run only after the y/n confirm accepts.

        Reached only when "current" is determinable, so a False from `stop_job`
        means no joined host pid (an unstoppable orphan) — surfaced honestly,
        separate from the degrade refusal above (R2 split).
        """
        ok = agent_ops.stop_job(job)
        if ok:
            self.app.notify("已发送停止信号（可能残留孤儿进程，请手动确认）")
        else:
            self.app.notify("找不到该后台 agent 的进程，无法停止")
        self.app.trigger_async_refresh()

    def _show_help(self) -> None:
        rows = [TextRow(line) for line in agent_ops.HELP.splitlines()]
        rows += [
            TextRow(""),
            TextRow("导航：Tab 切换标签 · q 退出"),
        ]
        self._mode = "help"
        self._show_overlay("后台 agent 帮助", rows)
        self._update_footer()
