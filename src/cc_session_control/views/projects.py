"""Projects view — the 项目 tab (evidence-tier membership + multi-CLI launcher).

Shows member projects (Project) — directories carrying ADR-0007 evidence
(pin / any-CLI trust / any-CLI activity). Enter opens the CLI chooser over
the ACTIVE providers, then starts a NEW session of the picked CLI in the
project dir inside tmux and enters it — the tmux-first launcher
(ADR-0001/0005; `x`/`k` jump straight to codex/kimi). `p`/`h`/`H` are the
curation verbs. The tab is a pure launcher: no service lifecycle lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import urwid

from ..actions import tui_actions
from ..actions.session_ops import TmuxNewIntent
from ..data import providers
from ..models import InventoryIssue, Project
from ._base import ListTabView
from ._colspec import ColSpec, header_columns, row_columns
from ._keytable import HelpLayout, Key, footer_hints, help_lines
from ._rows import SelectableRow, TextRow, dead_mapped

if TYPE_CHECKING:
    from ..app import App
    from ..data.refresh import RefreshBatch

# One spec drives the tab header + project rows (_colspec.py).
_PROJECT_COLS: list[ColSpec] = [
    (("weight", 2), "left", "项目"),
    (("weight", 3), "left", "目录"),
]


class _ProviderRow(SelectableRow):
    """One CLI-chooser row = one ACTIVE provider (Enter launches it)."""

    def __init__(self, provider_key: str) -> None:
        self.provider_key = provider_key
        super().__init__(dead_mapped(urwid.Text(f" {provider_key}")))


class ProjectRow(SelectableRow):
    def __init__(self, project: Project) -> None:
        self.project = project
        # Focus identity for the shared rebuild — activity ordering may move
        # this row between refreshes; the cursor follows the path key.
        self.row_key = project.directory
        directory = project.directory
        if not project.dir_exists:
            # Stale reference: membership evidence (or a pin) still lists the
            # project but its workspace directory is gone; launch is refused.
            directory += "（目录缺失）"
        cols = row_columns(
            _PROJECT_COLS,
            [
                project.name,
                directory,
            ],
        )
        super().__init__(dead_mapped(cols))


class ProjectsView(ListTabView):
    # Single source for every list-mode key (views/_keytable.py): footer,
    # help, and dispatch are generated from this table. `r 刷新` stays in the
    # App-level FOOTER_PREFIX, so its entry is hint-less.
    KEY_TABLE = (
        Key(
            ("enter",),
            "Enter 新建会话",
            "_key_tmux_new",
            section="项目操作:",
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
            section="项目操作:",
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
            section="项目操作:",
            help_lines=("  k      直达新建 tmux kimi 会话并进入（同上）",),
        ),
        Key(
            ("p",),
            "p 钉选",
            "_key_pin_toggle",
            section="项目操作:",
            help_lines=(
                "  p      钉选/取消钉选：钉选目录恒为项目，不受 temp/目录缺失",
                "         过滤与 30 天活动衰减影响（写入 csctl 自有配置）",
            ),
        ),
        Key(
            ("h",),
            "h 隐藏",
            "_key_hide_toggle",
            section="项目操作:",
            help_lines=(
                "  h      隐藏/取消隐藏：压制所有证据来源的自动纳入",
                "         （大写 H 切换显示已隐藏行，用于取消隐藏）",
            ),
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
        sections=("项目操作:", "批量操作:"),
        suffix=(
            "成员模型（ADR-0007 证据分层）:",
            "  项目 = 绝对目录 + 证据集：钉选（p）、任一 CLI 的信任记录、任一 CLI 的",
            "  会话活动（30 天无活动即出页）。temp 目录与已删目录不纳入，除非被钉选；",
            "  h 隐藏压制全部来源。",
            "目录缺失:",
            "  项目目录已删除时，只有被钉选的行默认仍显示（目录列标注）；",
            "  已隐藏记录在 H 模式仍可显示以便取消隐藏；缺失目录不能新建会话。",
            "",
            "导航:",
            "  Tab    切换标签页",
            "  q      退出",
        ),
    )

    def __init__(self, app: App) -> None:
        super().__init__(app, header_columns(_PROJECT_COLS))
        self._projects: list[Project] = []
        self._membership_issues: tuple[InventoryIssue, ...] = ()
        self._show_hidden = False
        self._help = False
        # CLI chooser (Enter): the project pinned when the chooser opened,
        # plus the overlay walker whose focused row is the picked provider.
        self._chooser: Project | None = None
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
        self._membership_issues = batch.snapshot.membership_issues
        self._loaded = True
        if not self._help and self._chooser is None:
            self._rebuild()

    def _build_rows(self) -> None:
        # Hidden rows ship in the scan (ADR-0007) but stay invisible until the
        # `H` show-hidden mode asks for them (that mode exists so `h` can
        # UNHIDE).
        for p in self._projects:
            if p.hidden and not self._show_hidden:
                continue
            self.walker.append(ProjectRow(p))
        if not self.walker:
            self.walker.append(urwid.AttrMap(urwid.Text(" 暂无项目"), "dead"))

    def _status_text(self) -> str:
        visible = [p for p in self._projects if not p.hidden]
        missing = sum(1 for p in visible if not p.dir_exists)
        miss_text = f" · 目录缺失 {missing}" if missing else ""
        hidden = len(self._projects) - len(visible)
        hidden_text = (
            f" · 已隐藏 {hidden}{'（列出中）' if self._show_hidden else ''}"
            if hidden
            else ""
        )
        membership_count = len(self._membership_issues)
        membership_text = (
            f" · ⚠ 项目来源异常 {membership_count}" if membership_count else ""
        )
        return f" 共 {len(visible)} 项目{miss_text}{hidden_text}{membership_text}"

    def _close_overlay_mode(self) -> None:
        self._help = False
        self._chooser = None

    def handle_key(self, key: str) -> None:
        """Chooser mode first (Enter confirms the focused provider, Esc
        cancels, ↑↓ stay with the overlay listbox); the help overlay + list
        dispatch fall through to the base handle_key."""
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

    def _selected(self) -> Project | None:
        widget = self._focused_widget()
        if isinstance(widget, ProjectRow):
            return widget.project
        return None

    # --- key handlers (bound by name in KEY_TABLE; dispatch lives in the base) ---

    def _key_tmux_new(self, p: Project) -> None:
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

    def _key_tmux_new_codex(self, p: Project) -> None:
        self._launch_new(p, "codex")

    def _key_tmux_new_kimi(self, p: Project) -> None:
        self._launch_new(p, "kimi")

    def _launch_new(self, p: Project, provider_key: str) -> None:
        """Shared multi-CLI launcher body (ADR-0005): pure spawn, no gates
        beyond directory existence and provider activation."""
        if not p.dir_exists:
            self.app.notify("目录缺失 — 无法新建会话")
            return
        if not providers.is_active(provider_key):
            self.app.notify(f"{provider_key} 未启用或未安装 — 无法新建会话")
            return
        self.app.exit_with(TmuxNewIntent(p.directory, provider=provider_key))

    def _key_pin_toggle(self, p: Project) -> None:
        """`p` — pin/unpin: a pinned directory stays a project regardless of
        hygiene and decay (csctl's own curation store, ADR-0007)."""
        path, name, pinned = p.directory, p.name, p.pinned
        self.app.submit_action(
            "project.pin",
            lambda: tui_actions.toggle_project_pin(path, name, not pinned),
        )

    def _key_hide_toggle(self, p: Project) -> None:
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

    def _show_help(self) -> None:
        """Help as a scrollable overlay, generated from KEY_TABLE."""
        self._help = True
        rows = [TextRow(line) for line in help_lines(self.KEY_TABLE, self.HELP_LAYOUT)]
        self._show_overlay("项目帮助", rows)
        self._update_footer()
