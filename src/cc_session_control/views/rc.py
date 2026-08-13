"""RC view — the 项目 tab (evidence-tier membership + the Remote Control surface).

Shows two things:
  1. member projects (RCProject) — directories carrying ADR-0007 evidence
     (pin / any-CLI trust / any-CLI activity), with typed
     `remoteControlAtStartup` evidence and `remoteControlSpawnMode`, plus
     Enter (open the CLI chooser over the ACTIVE providers, then start a NEW
     session of the picked CLI in the project dir inside tmux and enter it —
     the tmux-first launcher, ADR-0001/0005; `x`/`k` jump straight to
     codex/kimi), `o` (start the project RC server, the demoted secondary —
     Claude-trust gated), the stop / auto-RC keys, and the `p`/`h`/`H`
     curation verbs;
  2. project RC servers (RCServer) discovered via tmux ∪ /proc, badged
     managed/external — external servers are READ-ONLY (no takeover/restart key).

Only project rows are actionable. Server rows are display-only, so no key
toggles RC on a running session or takes over an external server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..actions import tui_actions
from ..actions.session_ops import TmuxNewIntent
from ..data import providers
from ..data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from ..models import InventoryIssue, RCProject, RCServer, TrustDecision
from ._base import ListTabView
from ._colspec import ColSpec, header_columns, row_columns
from ._confirm import confirm_stop
from ._keytable import HelpLayout, Key, footer_hints, help_lines
from ._rc_rows import RC_FOCUS, STATUS_ATTR, STATUS_MAP, DividerRow, ServerRow
from ._rows import SelectableRow, TextRow

if TYPE_CHECKING:
    from ..app import App
    from ..data.refresh import RefreshBatch

_RC_TRISTATE = {True: "开", False: "关", None: "未设置"}
# `c` cycles the per-project remoteControlAtStartup tri-state in full so the user
# can return to an explicit True (the old 2-cycle could never set True again).
_NEXT_TRISTATE = {None: True, True: False, False: None}

# One spec drives the tab header + project rows (_colspec.py).
# 证据 width fits ~4 CJK-prefixed tokens (信cc/活km ≈ 6 display cols each);
# widened for ADR-0008, where several codex identities can each contribute
# 信/活 tokens. Longer provenance still truncates rather than wrapping.
_PROJECT_COLS: list[ColSpec] = [
    (10, "left", "状态"),
    (8, "left", "自动远控"),
    (10, "left", "启动模式"),
    (24, "left", "证据"),
    (("weight", 2), "left", "项目"),
    (("weight", 3), "left", "目录"),
]


def _evidence_text(project: RCProject) -> str:
    """Provenance badges (ADR-0007): 钉/隐 markers, then per-CLI 信/活 tokens.

    信<x> = that CLI's trust store covers the directory (信cc ⇔ the RC start
    gate passes); 活<x> = that CLI has session activity in it.
    """
    tokens: list[str] = []
    if project.pinned:
        tokens.append("钉")
    if project.hidden:
        tokens.append("隐")
    for provider in providers.all_providers():
        if provider.key in project.trusted_by:
            tokens.append(f"信{provider.label}")
        if provider.key in project.observed_by:
            tokens.append(f"活{provider.label}")
    return " ".join(tokens) or "—"


class _ProviderRow(SelectableRow):
    """One CLI-chooser row = one ACTIVE provider (Enter launches it)."""

    def __init__(self, provider_key: str) -> None:
        self.provider_key = provider_key
        mapped = urwid.AttrMap(
            urwid.Text(f" {provider_key}"),
            "dead",
            focus_map={"dead": "selected", None: "selected"},
        )
        super().__init__(mapped)


class RCRow(SelectableRow):
    def __init__(self, project: RCProject) -> None:
        self.project = project
        # Focus identity for the shared rebuild — activity ordering may move
        # this row between refreshes; the cursor follows the path key.
        self.row_key = project.directory
        status_text = STATUS_MAP.get(project.status, project.status)
        attr = STATUS_ATTR.get(project.status, "dead")
        directory = project.directory
        if not project.dir_exists:
            # Stale reference: claude.json still lists the project but its
            # workspace directory is gone. A running server (dir deleted
            # underneath it) keeps its status; otherwise 缺失 wins.
            directory += "（目录缺失）"
            if project.status != "running":
                status_text = "✖ 缺失"
                attr = "status_err"
        rc_at = (
            _RC_TRISTATE[project.rc_at_startup]
            if project.rc_at_startup_setting.available
            else "读取失败"
        )
        spawn = project.spawn_mode or "—"

        cols = row_columns(
            _PROJECT_COLS,
            [
                status_text,
                rc_at,
                spawn,
                _evidence_text(project),
                project.name,
                directory,
            ],
        )
        mapped = urwid.AttrMap(cols, attr, focus_map=RC_FOCUS)
        super().__init__(mapped)


class RCView(ListTabView):
    # Single source for every list-mode key (views/_keytable.py): footer,
    # help, and dispatch are generated from this table. `r 刷新` stays in the
    # App-level FOOTER_PREFIX, so its entry is hint-less.
    KEY_TABLE = (
        Key(
            ("enter",),
            "Enter 新建会话",
            "_key_tmux_new",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  Enter  新建会话：先弹 CLI 选择器（仅列已启用的 CLI，↑↓ 选择，",
                "         默认选中首行 claude，Esc 取消），再次 Enter 确认后在统一",
                "         csctl tmux session 新建项目窗口并直接进入（离开 csctl；",
                "         tmux-first",
                "         主入口，会话默认获得断线保护）",
            ),
        ),
        Key(
            ("x",),
            "x 新codex",
            "_key_tmux_new_codex",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  x      直达新建 tmux codex 会话并进入（跳过选择器；多 CLI",
                "         启动器，ADR-0005；codex 未启用时拒绝。声明了多个",
                "         codex 身份时，x 走默认身份，其余身份用 Enter 选择器）",
            ),
        ),
        Key(
            ("k",),
            "k 新kimi",
            "_key_tmux_new_kimi",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=("  k      直达新建 tmux kimi 会话并进入（同上）",),
        ),
        Key(
            ("o",),
            "o 启动远控",
            "_key_start",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  o      启动选中项目的远程控制服务（手机/网页控制面，次要入口）",
            ),
        ),
        Key(
            ("s",),
            "s 停止",
            "_key_stop",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=("  s      停止选中项目的远程控制服务（需确认）",),
        ),
        Key(
            ("c",),
            "c 自动远控",
            "_key_rc_toggle",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  c      切换「自动远控」：claude 启动时自动开远程控制，手机即可接管",
            ),
        ),
        Key(
            ("p",),
            "p 钉选",
            "_key_pin_toggle",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  p      钉选/取消钉选：钉选目录恒为项目，不受 temp/目录缺失",
                "         过滤与 30 天活动衰减影响（写入 csctl 自有配置）",
            ),
        ),
        Key(
            ("h",),
            "h 隐藏",
            "_key_hide_toggle",
            section="项目操作（仅对「项目」行生效）:",
            help_lines=(
                "  h      隐藏/取消隐藏：压制所有证据来源的自动纳入",
                "         （大写 H 切换显示已隐藏行，用于取消隐藏）",
            ),
        ),
        Key(
            ("S",),
            "S 全部停止",
            "_key_stop_all",
            needs_selection=False,
            section="批量操作:",
            help_lines=("  S      停止全部远程控制服务（需确认）",),
        ),
        Key(
            ("H",),
            "H 显隐藏",
            "_key_toggle_show_hidden",
            needs_selection=False,
            section="批量操作:",
            help_lines=("  H      切换是否列出已隐藏的项目（列出时可按 h 取消隐藏）",),
        ),
        Key(
            ("r",),
            None,
            "_key_refresh",
            needs_selection=False,
            section="批量操作:",
            help_lines=("  r      重新扫描刷新",),
        ),
        Key(("?",), "? 详细说明", "_show_help", needs_selection=False),
    )

    HELP_LAYOUT = HelpLayout(
        sections=("项目操作（仅对「项目」行生效）:", "批量操作:"),
        suffix=(
            "成员模型（ADR-0007 证据分层）:",
            "  项目 = 绝对目录 + 证据集：钉选（p）、任一 CLI 的信任记录（信cc/信cx/信km）、",
            "  任一 CLI 的会话活动（活cc/活cx/活km，30 天无活动即出页）。temp 目录与",
            "  已删目录不纳入，除非被钉选或仍持有远控窗口；h 隐藏压制全部来源。",
            "  RC 服务仍是 Claude 专属：仅 信cc 的目录可启动远控。",
            "目录缺失（✖ 缺失）:",
            "  项目目录已删除但其远控服务窗口还在、或被钉选时仍显示；停止该服务后此行消失。",
            "  只剩 claude 信任记录（~/.claude.json）引用的已删项目不再显示；信任记录需手动清理。",
            "",
            "RC 服务（只读）:",
            "  外部服务只展示，不接管、不重启。",
            "",
            "导航:",
            "  Tab    切换标签页",
            "  q      退出",
        ),
    )

    def __init__(self, app: App) -> None:
        super().__init__(app, header_columns(_PROJECT_COLS))
        self._projects: list[RCProject] = []
        self._servers: list[RCServer] = []
        self._settings = ProjectSettingsResult(ProjectSettingsState.MISSING, {})
        self._inventory_issues: tuple[InventoryIssue, ...] = ()
        self._membership_issues: tuple[InventoryIssue, ...] = ()
        self._show_hidden = False
        self._help = False
        # CLI chooser (Enter): the project pinned when the chooser opened,
        # plus the overlay walker whose focused row is the picked provider.
        self._chooser: RCProject | None = None
        self._chooser_walker = urwid.SimpleFocusListWalker([])

    def keyhints(self) -> str:
        if self._chooser is not None:
            return "选择 CLI · Enter 新建会话 · Esc 取消"
        if self._overlay_active():
            # "其余" is honest: the prefix's Tab/q stay global (Tab switches
            # tabs, q QUITS — neither returns to the list).
            return "其余任意键返回"
        # Every list-mode key gets a brief hint, straight from KEY_TABLE; the
        # footer Text wraps on narrow terminals (vertical for width).
        return footer_hints(self.KEY_TABLE)

    def _overlay_active(self) -> bool:
        return self._help

    def apply_refresh(self, batch: RefreshBatch) -> None:
        """Apply one complete generation on the urwid main loop."""
        self._projects = list(batch.ordered_projects)
        self._settings = batch.snapshot.rc_project_settings
        self._servers = list(batch.snapshot.rc_servers)
        self._inventory_issues = batch.snapshot.rc_inventory_issues
        self._membership_issues = batch.snapshot.membership_issues
        self._loaded = True
        if not self._help and self._chooser is None:
            self._rebuild()

    def _build_rows(self) -> None:
        # Projects first, so default focus lands on an actionable row. Hidden
        # rows ship in the scan (ADR-0007) but stay invisible until the `H`
        # show-hidden mode asks for them (that mode exists so `h` can UNHIDE).
        for p in self._projects:
            if p.hidden and not self._show_hidden:
                continue
            self.walker.append(RCRow(p))
        if self._servers:
            self.walker.append(
                DividerRow("── RC 服务（仅展示 · 托管见项目行 · 外部不可接管）──")
            )
            for s in self._servers:
                self.walker.append(ServerRow(s))
        if not self.walker:
            self.walker.append(urwid.AttrMap(urwid.Text(" 暂无远控项目"), "dead"))

    def _status_text(self) -> str:
        visible = [p for p in self._projects if not p.hidden]
        running = sum(1 for p in visible if p.status == "running")
        rc_off = sum(1 for p in visible if p.rc_at_startup is False)
        rc_errors = sum(1 for p in visible if not p.rc_at_startup_setting.available)
        missing = sum(1 for p in visible if not p.dir_exists)
        rc_text = f" · 自动远控关 {rc_off}" if rc_off else ""
        rc_error_text = f" · 自动远控异常 {rc_errors}" if rc_errors else ""
        miss_text = f" · 目录缺失 {missing}" if missing else ""
        srv_text = f" · 服务 {len(self._servers)}" if self._servers else ""
        hidden = len(self._projects) - len(visible)
        hidden_text = (
            f" · 已隐藏 {hidden}{'（列出中）' if self._show_hidden else ''}"
            if hidden
            else ""
        )
        settings_text = (
            f" · 项目设置不可用（{self._settings.state.value}）"
            if not self._settings.available
            else ""
        )
        inventory_count = len(self._inventory_issues)
        inventory_text = (
            f" · ⚠ RC 清单不完整 {inventory_count}" if inventory_count else ""
        )
        membership_count = len(self._membership_issues)
        membership_text = (
            f" · ⚠ 项目来源异常 {membership_count}" if membership_count else ""
        )
        return (
            f" 共 {len(visible)} 项目 · 运行 {running}"
            f"{rc_text}{rc_error_text}{miss_text}{srv_text}{hidden_text}"
            f"{settings_text}{inventory_text}{membership_text}"
        )

    def _close_overlay_mode(self) -> None:
        self._help = False
        self._chooser = None

    def handle_key(self, key: str) -> None:
        """Chooser mode first (Projects-only — Enter confirms the focused
        provider, Esc cancels, ↑↓ stay with the overlay listbox); the help
        overlay + list dispatch fall through to the base handle_key."""
        if self._chooser is not None:
            if key == "enter":
                self._confirm_chooser()
            elif key == "esc":
                self._exit_overlay()
            elif key == "r":
                # Footer prefix promises `r 刷新` on every tab/mode — honor it.
                self.app.refresh_with_notice()
            return
        super().handle_key(key)

    def _confirm_chooser(self) -> None:
        """Launch the focused provider for the project pinned at chooser open."""
        project = self._chooser
        widget = self._chooser_walker.get_focus()[0] if self._chooser_walker else None
        self._exit_overlay()
        if project is None or not isinstance(widget, _ProviderRow):
            return
        self._launch_new(project, widget.provider_key)

    def _selected(self) -> RCProject | None:
        widget = self._focused_widget()
        if isinstance(widget, RCRow):
            return widget.project
        return None

    # --- key handlers (bound by name in KEY_TABLE; dispatch lives in the base) ---

    def _key_tmux_new(self, p: RCProject) -> None:
        """Enter — CLI 选择器 (ADR-0005 amendment, 2026-08-04): arrows + Enter
        pick one ACTIVE provider, then launch through the same `_launch_new`
        path as the x/k direct shortcuts. Registry order puts claude first,
        so default first-row focus keeps Enter-Enter ≡ the old direct
        claude launch; Esc cancels back to the list."""
        if not p.dir_exists:
            self.app.notify("目录缺失 — 无法新建会话")
            return
        active = providers.active_providers()
        if not active:
            self.app.notify("没有已启用的 CLI — 无法新建会话")
            return
        self._chooser = p
        rows: list[urwid.Widget] = [_ProviderRow(prov.key) for prov in active]
        self._chooser_walker = self._show_overlay("选择 CLI 新建会话", rows)
        self._update_footer()

    def _key_tmux_new_codex(self, p: RCProject) -> None:
        self._launch_new(p, "codex")

    def _key_tmux_new_kimi(self, p: RCProject) -> None:
        self._launch_new(p, "kimi")

    def _launch_new(self, p: RCProject, provider_key: str) -> None:
        """Shared multi-CLI launcher body (ADR-0005): pure spawn, no gates
        beyond directory existence and provider activation."""
        if not p.dir_exists:
            self.app.notify("目录缺失 — 无法新建会话")
            return
        if not providers.is_active(provider_key):
            self.app.notify(f"{provider_key} 未启用或未安装 — 无法新建会话")
            return
        self.app.exit_with(TmuxNewIntent(p.directory, provider=provider_key))

    def _key_start(self, p: RCProject) -> None:
        if not p.dir_exists:
            self.app.notify("目录缺失 — 无法启动")
            return
        if p.trust_decision is TrustDecision.UNAVAILABLE:
            self.app.notify("项目设置不可用 — 已拒绝启动")
            return
        if p.trust_decision is TrustDecision.UNTRUSTED:
            self.app.notify("未信任 — 先在该目录跑一次 claude")
            return
        if p.status == "unknown":
            self.app.notify("RC 清单不可用 — 已拒绝启动")
            return
        if p.status == "running":
            self.app.notify("已在运行")
            return
        path, name = p.directory, p.name
        self.app.submit_action(
            "project.start",
            lambda: tui_actions.start_project(path, name),
        )

    def _key_stop(self, p: RCProject) -> None:
        if p.status == "unknown":
            self.app.notify("RC 清单不可用 — 无法确认是否运行")
            return
        # gated=False: this stop kills a tmux window, not a pid — no R10 gate.
        confirm_stop(
            self.app,
            "远控服务",
            p.name,
            lambda: self._do_stop_one(p),
            alive=p.status == "running",
            gated=False,
        )

    def _key_rc_toggle(self, p: RCProject) -> None:
        if not p.dir_exists:
            # write_rc_at_startup would mkdir the deleted project back to life.
            self.app.notify("目录缺失 — 不写入配置")
            return
        setting = p.rc_at_startup_setting
        if not setting.available:
            source = f"：{setting.source}" if setting.source is not None else ""
            detail = f"：{setting.detail}" if setting.detail else ""
            self.app.notify(
                f"自动远控配置不可用（{setting.state.value}）"
                f"{source}{detail} — 不写入配置"
            )
            return
        # Full 3-cycle so explicit True is reachable again: None→True→False→None.
        new = _NEXT_TRISTATE[p.rc_at_startup]
        path, name = p.directory, p.name
        self.app.submit_action(
            "project.write-settings",
            lambda: tui_actions.write_auto_rc(path, name, new),
        )

    def _key_pin_toggle(self, p: RCProject) -> None:
        """`p` — pin/unpin: a pinned directory stays a project regardless of
        hygiene and decay (csctl's own curation store, ADR-0007)."""
        path, name, pinned = p.directory, p.name, p.pinned
        self.app.submit_action(
            "project.pin",
            lambda: tui_actions.toggle_project_pin(path, name, not pinned),
        )

    def _key_hide_toggle(self, p: RCProject) -> None:
        """`h` — hide/unhide: hidden suppresses every evidence tier until the
        operator unhides via the `H` show-hidden mode."""
        path, name, hidden = p.directory, p.name, p.hidden
        self.app.submit_action(
            "project.hide",
            lambda: tui_actions.toggle_project_hidden(path, name, not hidden),
        )

    def _key_toggle_show_hidden(self) -> None:
        """`H` — list hidden rows so the `h` verb can unhide them."""
        self._show_hidden = not self._show_hidden
        self._rebuild()

    def _key_stop_all(self) -> None:
        if any(p.status == "unknown" for p in self._projects):
            self.app.notify("RC 清单不可用 — 无法确认是否运行")
            return
        if not any(p.status == "running" for p in self._projects):
            self.app.notify("本来就没在跑")
            return
        self.app.confirm("停止全部远控服务？", self._do_stop_all)

    def _do_stop_one(self, p: RCProject) -> None:
        """Stop-one body, run only after the y/n confirm accepts."""
        path, name = p.directory, p.name
        self.app.submit_action(
            "project.stop",
            lambda: tui_actions.stop_project(path, name),
        )

    def _do_stop_all(self) -> None:
        """Stop-all body, run only after the y/n confirm accepts."""
        self.app.submit_action(
            "project.stop-all",
            tui_actions.stop_all_projects,
        )

    def _show_help(self) -> None:
        """Help as a scrollable overlay, generated from KEY_TABLE."""
        self._help = True
        rows = [TextRow(line) for line in help_lines(self.KEY_TABLE, self.HELP_LAYOUT)]
        self._show_overlay("项目 / 远控帮助", rows)
        self._update_footer()
