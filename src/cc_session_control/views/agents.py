"""Agents view — the 后台 (background agents) tab.

Lists `jobs/<short>/state.json` records (registry.AgentJob, enriched with host
liveness) and wires their lifecycle to `actions/agent_ops`: respawn, takeover
(via the existing resume path), read-only watch, settled-only remove, and
live-only stop. Satisfies the TabView Protocol structurally so `app.py` drives
it generically. All user-facing strings are Simplified Chinese; the orphan-risk
warning surfaced on `stop` is a capability red line (R4.5 / AC4).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import urwid

from ..actions import agent_ops, tui_actions
from ..actions.session_ops import ResumeIntent
from ..data import proc
from ..models import AgentJob
from ._base import ListTabView
from ._colspec import ColSpec, header_columns, row_columns
from ._confirm import DEGRADED as _DEGRADED
from ._confirm import confirm_stop, confirm_takeover, confirm_tmux_takeover
from ._keytable import HelpLayout, Key, footer_hints, help_lines
from ._rows import TextRow

if TYPE_CHECKING:
    from ..app import App
    from ..data.refresh import RefreshBatch


# One spec drives header + rows (_colspec.py). The 状态 text column already
# carries the state in words, so the mark column only needs the ●/○ shape —
# state never rides on color alone here either.
_AGENT_COLS: list[ColSpec] = [
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
        cols = row_columns(
            _AGENT_COLS,
            [
                mark,
                job.name or job.short,
                job.state or "-",
                job.tempo or "-",
                cwd,
                job.env_suffix or "-",
            ],
        )
        attr = "alive" if job.host_alive else "dead"
        mapped = urwid.AttrMap(
            cols,
            attr,
            focus_map={"alive": "selected", "dead": "selected", None: "selected"},
        )
        super().__init__(mapped)

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple, key: str) -> str | None:
        return key


class AgentsView(ListTabView):
    # mode: "list" | "help" | "watch"

    # Single source for every list-mode key (views/_keytable.py): footer,
    # help, and dispatch are generated from this table. `r 刷新` stays in the
    # App-level FOOTER_PREFIX, so its entry is hint-less.
    KEY_TABLE = (
        Key(
            ("enter",),
            "Enter 接回",
            "_takeover",
            section="后台 agent 生命周期:",
            help_lines=(
                "  Enter   tmux 接回（恢复进 tmux 窗口并接入前台，断线不死；",
                "          已驻留 tmux 的 agent 就地进入；接运行中的会先确认接管）",
            ),
        ),
        Key(
            ("t",),
            "t 终端接回",
            "_terminal",
            section="后台 agent 生命周期:",
            help_lines=(
                "  t       终端接回（在当前终端恢复，随终端关闭而结束——兜底；",
                "          接运行中的会先确认接管）",
            ),
        ),
        Key(
            ("s",),
            "s 停止",
            "_stop",
            section="后台 agent 生命周期:",
            help_lines=("  s       停止（仅运行中，需确认）",),
        ),
        Key(
            ("d",),
            "d 删除",
            "_remove",
            section="后台 agent 生命周期:",
            help_lines=("  d       删除（仅已结束）",),
        ),
        Key(
            ("w",),
            "w 查看",
            "_watch",
            section="后台 agent 生命周期:",
            help_lines=("  w       查看 timeline（只读）",),
        ),
        Key(
            ("R",),
            "R 重启",
            "_key_respawn",
            section="后台 agent 生命周期:",
            help_lines=("  R       重启（respawn）",),
        ),
        Key(
            ("r",),
            None,
            "_key_refresh",
            needs_selection=False,
            section="后台 agent 生命周期:",
            help_lines=("  r       刷新",),
        ),
        Key(("?",), "? 详细说明", "_show_help", needs_selection=False),
    )

    # Orphan-process risk (R4.5 red line): stop only kills the host pid joined
    # from the sessions registry — killing a --remote-control/bg worker does
    # not always fully reap it, and a live worker with no sessions file can't
    # be located at all.
    HELP_LAYOUT = HelpLayout(
        sections=("后台 agent 生命周期:",),
        suffix=(
            "停止/孤儿风险:",
            "  停止只能杀经 sessions 文件 join 到的 host pid；杀 --remote-control/",
            "  后台 agent 不一定彻底回收，可能残留孤儿进程，需手动确认；",
            "  找不到运行中的后台 agent 的 host pid 时无法停止。",
            "",
            "导航：Tab 切换标签 · q 退出",
        ),
    )

    def __init__(self, app: App) -> None:
        super().__init__(app, _AGENTS_HEADER)
        self._jobs: Sequence[AgentJob] = ()
        self._mode = "list"

    # --- TabView contract ---

    def keyhints(self) -> str:
        if self._overlay_active():
            # "其余" is honest: the prefix's Tab/q stay global (Tab switches
            # tabs, q QUITS — neither returns to the list).
            return "其余任意键返回"
        return footer_hints(self.KEY_TABLE)

    def _overlay_active(self) -> bool:
        return self._mode in ("help", "watch")

    def apply_refresh(self, batch: RefreshBatch) -> None:
        """Apply one complete generation on the urwid main loop."""
        self._jobs = batch.snapshot.agent_jobs
        self._loaded = True
        if self._mode == "list":
            self._rebuild()

    # --- rendering ---

    def _build_rows(self) -> None:
        for job in self._jobs:
            self.walker.append(AgentRow(job))
        if not self._jobs:
            self.walker.append(urwid.AttrMap(urwid.Text(" 暂无后台 agent"), "dead"))

    def _status_text(self) -> str:
        alive_n = sum(1 for j in self._jobs if j.host_alive)
        return f" 共 {len(self._jobs)} 个后台 agent · 运行 {alive_n}"

    def _close_overlay_mode(self) -> None:
        self._mode = "list"

    def _selected(self) -> AgentJob | None:
        widget = self._focused_widget()
        if isinstance(widget, AgentRow):
            return widget.job
        return None

    # --- key handlers (bound by name in KEY_TABLE; dispatch lives in the base) ---

    def _key_respawn(self, job: AgentJob) -> None:
        request = tui_actions.AgentRequest.from_job(job)
        self.app.submit_action(
            "agent.respawn",
            lambda: tui_actions.respawn_agent(request),
        )

    def _takeover(self, job: AgentJob) -> None:
        """Enter — tmux 接回: a tmux-resident worker is entered in place;
        otherwise resume it inside its per-project tmux window (ADR-0001)."""
        s = agent_ops.resume_takeover(job)
        if s.current:
            self.app.notify("不能接回当前会话")
            return
        # B1: takeover of a RUNNING worker kills its host pid (should_kill) — same
        # as Sessions Enter-live. A dead worker resumes directly, unconfirmed.
        confirm_tmux_takeover(
            self.app,
            s,
            "接回后台 agent",
            name=job.name or job.short,
        )

    def _terminal(self, job: AgentJob) -> None:
        """t — 终端接回 (fallback): bare-terminal resume via the existing path."""
        s = agent_ops.resume_takeover(job)
        if s.current:
            self.app.notify("不能接回当前会话")
            return
        confirm_takeover(
            self.app,
            s,
            "终端接回后台 agent",
            lambda: self.app.exit_with(ResumeIntent(s)),
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
        except OSError:
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
        request = tui_actions.AgentRequest.from_job(job)
        self.app.submit_action(
            "agent.remove",
            lambda: tui_actions.remove_agent(request),
        )

    def _stop(self, job: AgentJob) -> None:
        confirm_stop(
            self.app,
            "后台 agent",
            job.name or job.short,
            lambda: self._do_stop(job),
            alive=job.host_alive,
        )

    def _do_stop(self, job: AgentJob) -> None:
        """Stop body, run only after the y/n confirm accepts.

        Reached only when "current" is determinable, so a False from `stop_job`
        means no joined host pid (an unstoppable orphan) — surfaced honestly,
        separate from the degrade refusal above (R2 split).
        """
        request = tui_actions.AgentRequest.from_job(job)
        self.app.submit_action(
            "agent.stop",
            lambda: tui_actions.stop_agent(request),
        )

    def _show_help(self) -> None:
        rows = [TextRow(line) for line in help_lines(self.KEY_TABLE, self.HELP_LAYOUT)]
        self._mode = "help"
        self._show_overlay("后台 agent 帮助", rows)
        self._update_footer()
