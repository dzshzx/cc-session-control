"""View unit tests — construct widgets and verify basic behavior without MainLoop."""

import ast
import os
from pathlib import Path

import pytest
import urwid

from cc_session_control.actions.runner import Accepted
from cc_session_control.actions.session_ops import (
    AttachIntent,
    ResumeIntent,
    TmuxNewIntent,
    TmuxResumeIntent,
)
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
    SettingWriteFailure,
    SettingWriteResult,
    SettingWriteState,
)
from cc_session_control.data.refresh import RefreshBatch
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import (
    RCProject,
    RCServer,
    RCStartupSettingRead,
    RCStartupSettingState,
    Session,
)
from cc_session_control.views.rc import RCRow, RCView, ServerRow
from cc_session_control.views.sessions import SessionRow, SessionsView


class FakeApp:
    """Minimal stub for App used by views."""

    def __init__(self):
        self.result = None
        self._notifications = []
        self._confirm_messages = []
        self._last_confirm = None
        self._submitted_actions = []
        self.footer_text = urwid.Text("")
        self.footer = urwid.AttrMap(self.footer_text, "footer")
        self.frame = urwid.Frame(urwid.Text("body"), footer=self.footer)
        self.views = []
        self._active = 0

    def notify(self, msg, seconds=3):
        self._notifications.append(msg)

    def confirm(self, message, on_yes):
        # Mirror App.confirm: record the prompt and capture the callback so a test
        # can simulate pressing `y` via `app._last_confirm()`.
        self._confirm_messages.append(message)
        self._last_confirm = on_yes

    def exit_with(self, intent):
        self.result = intent

    def trigger_async_refresh(self):
        pass

    def submit_action(self, action_key, action):
        self._submitted_actions.append(action_key)
        result = action()
        self.notify(result.message)
        if result.needs_refresh:
            self.trigger_async_refresh()
        return Accepted(action_key)

    def refresh_with_notice(self):
        self.trigger_async_refresh()
        self.notify("刷新中…")

    def set_hints(self, hints):
        self.footer_text.set_text(hints)

    def _restore_footer(self):
        self.frame.footer = self.footer

    def is_active(self, view):
        return not self.views or self.views[self._active] is view


def _make_session(**overrides):
    defaults = dict(
        sid="abc123",
        cwd="/tmp/proj",
        label="test session",
        mtime=1700000000.0,
        prompts=5,
        pid=None,
        alive=False,
        current=False,
        hidden=set(),
        file="/tmp/abc123.jsonl",
    )
    defaults.update(overrides)
    return Session(**defaults)


def _make_project(**overrides):
    defaults = dict(
        name="myproj",
        directory="/tmp/myproj",
        trusted=True,
        in_list=True,
        status="stopped",
        auto_start=True,
    )
    if "rc_at_startup" in overrides:
        value = overrides.pop("rc_at_startup")
        state = {
            None: RCStartupSettingState.UNSET,
            True: RCStartupSettingState.TRUE,
            False: RCStartupSettingState.FALSE,
        }[value]
        overrides["rc_at_startup_setting"] = RCStartupSettingRead(state)
    defaults.update(overrides)
    return RCProject(**defaults)


def _row_text(row):
    canvas = row.render((120,), focus=False)
    return b"\n".join(canvas.text).decode()


def _updated_setting(directory):
    return SettingWriteResult(
        SettingWriteState.UPDATED,
        Path(directory) / ".claude" / "settings.local.json",
    )


def _refresh_batch(
    snapshot: WorldSnapshot | None = None,
    *,
    plan: CleanupPlan | None = None,
    ordered_projects: tuple[RCProject, ...] | None = None,
) -> RefreshBatch:
    snapshot = snapshot or WorldSnapshot()
    plan = plan or CleanupPlan()
    counts = plan.counts()
    return RefreshBatch(
        generation=1,
        snapshot=snapshot,
        cleanup_plan=plan,
        cleanup_counts=counts,
        session_stats={
            "total": len(snapshot.sessions),
            "empty": counts["empty"],
            "short": counts["short"],
            "orphans": counts["orphan_dirs"],
        },
        ordered_projects=(
            ordered_projects
            if ordered_projects is not None
            else tuple(snapshot.rc_projects)
        ),
    )


def _apply_projects(
    view: RCView,
    projects: list[RCProject],
    *,
    settings: ProjectSettingsResult | None = None,
    servers: list[RCServer] | None = None,
) -> None:
    snapshot = WorldSnapshot(
        rc_projects=projects,
        rc_project_settings=(
            settings
            if settings is not None
            else ProjectSettingsResult(ProjectSettingsState.MISSING, {})
        ),
        rc_servers=servers or [],
    )
    view.apply_refresh(_refresh_batch(snapshot, ordered_projects=tuple(projects)))


def test_views_satisfy_tabview_protocol():
    from cc_session_control.app import TabView

    assert isinstance(SessionsView(FakeApp()), TabView)
    assert isinstance(RCView(FakeApp()), TabView)


def test_session_row_selectable():
    s = _make_session()
    row = SessionRow(s)
    assert row.selectable()
    assert row.session.sid == "abc123"


def test_session_row_alive_vs_dead():
    alive = SessionRow(_make_session(alive=True, pid=1234))
    dead = SessionRow(_make_session(alive=False))
    assert alive.session.alive
    assert not dead.session.alive


def test_session_row_labels_hidden_bridge_and_sdk_sessions():
    row = SessionRow(_make_session(label="phone session", hidden={"bridge", "sdk"}))
    text = _row_text(row)
    assert "[桥接 SDK] phone session" in text


def test_sessions_view_construct():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    assert view.widget is not None
    assert len(view.walker) == 0


def test_sessions_view_filter_logic():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="a1", label="deploy fix"),
        _make_session(sid="a2", label="config change"),
        _make_session(sid="a3", label="deploy rollback"),
    ]
    view._filter_text = "deploy"
    view._apply_filter()
    assert len(view._sessions) == 2
    view._filter_text = ""
    view._apply_filter()
    assert len(view._sessions) == 3


def test_sessions_view_shows_hidden_sessions_by_default():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", hidden=set()),
        _make_session(sid="bridge", hidden={"bridge"}),
    ]

    view._apply_filter()

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]


def test_sessions_view_h_key_toggles_hidden_sessions():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", hidden=set()),
        _make_session(sid="bridge", hidden={"bridge"}),
    ]
    view._apply_filter()
    view._rebuild()

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]
    assert "桥接/SDK 1" in view.status.original_widget.get_text()[0]

    view.handle_key("h")

    assert [s.sid for s in view._sessions] == ["normal"]
    assert "桥接/SDK已隐藏 1" in view.status.original_widget.get_text()[0]
    # `h` moved into `?` help (D3 footer slim-down) — no longer in the footer hint.

    view.handle_key("h")

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]


def test_sessions_view_filter_respects_hidden_toggle():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", label="plain deploy", hidden=set()),
        _make_session(sid="bridge", label="mobile deploy", hidden={"bridge"}),
    ]
    view._filter_text = "deploy"

    view._apply_filter()

    assert [s.sid for s in view._sessions] == ["normal", "bridge"]

    view._show_hidden = False
    view._apply_filter()

    assert [s.sid for s in view._sessions] == ["normal"]


def test_sessions_view_filter_mode_routes_text_to_edit():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]

    view.handle_key("/")
    view.handle_key("d")

    assert view._filter_edit.get_edit_text() == "d"


def test_sessions_cleanup_mode(monkeypatch):
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._classified = {
        "empty": 10,
        "short": 5,
        "orphan_dirs": 3,
        "zombie_procs": 2,
        "aged_entries": 4,
    }
    view._enter_cleanup()
    assert view._mode == "cleanup"
    # Five submenu actions now: empty/short/orphans/zombies/aged (CLI/TUI parity).
    assert len(view._cleanup_walker) == 5
    view._exit_cleanup()
    assert view._mode == "list"


def test_sessions_short_cleanup_preview_reads_frozen_plan(monkeypatch):
    # The preview list comes from the frozen CleanupPlan — no re-scan on entry.
    import cc_session_control.views._sessions_cleanup as cl_mod
    from cc_session_control.data.cleanup import CleanupPlan

    monkeypatch.setattr(cl_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(
        short=[
            _make_session(sid="short1", prompts=1),
            _make_session(sid="short2", prompts=2),
        ]
    )

    view._enter_preview("short")

    assert {s.sid for s in view._preview_targets} == {"short1", "short2"}


def _sessions_view_with(monkeypatch, session):
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._sessions = [session]
    view._all_sessions = [session]
    view._rebuild()
    return app, view


def test_t_key_refuses_current_session(monkeypatch):
    s = _make_session(sid="cur", alive=True, current=True, pid=1)
    app, view = _sessions_view_with(monkeypatch, s)
    view.handle_key("t")
    assert app.result is None
    assert "不能接回当前会话" in app._notifications[-1]


def test_enter_key_attaches_resident_session_without_confirm(monkeypatch):
    # ADR-0001: Enter on a tmux-resident session enters it IN PLACE — no kill,
    # no confirm. Residency comes from the snapshot field, not a live lookup.
    s = _make_session(
        sid="sid1", alive=True, current=False, pid=4242, tmux_target="cc:2"
    )
    app, view = _sessions_view_with(monkeypatch, s)
    view.handle_key("enter")
    assert app.result == AttachIntent("cc:2")
    assert app._confirm_messages == []


def test_t_key_live_confirms_terminal_takeover(monkeypatch):
    # t = 终端接回 (bare-terminal fallback): a live session — resident or not —
    # goes through the standard takeover confirm into ResumeIntent.
    import cc_session_control.views.sessions as sv_mod

    s = _make_session(
        sid="sid1", alive=True, current=False, pid=4242, tmux_target="cc:2"
    )
    app, view = _sessions_view_with(monkeypatch, s)
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    view.handle_key("t")
    assert app.result is None  # blocked on confirm (pull OUT of tmux = takeover)
    assert "终端接回" in app._confirm_messages[-1]
    app._last_confirm()
    assert app.result == ResumeIntent(s, fork=False)


def test_t_key_dead_session_no_confirm(monkeypatch):
    s = _make_session(sid="sid1", alive=False, pid=None)
    app, view = _sessions_view_with(monkeypatch, s)
    view.handle_key("t")
    assert app.result == ResumeIntent(s, fork=False)
    assert app._confirm_messages == []


def test_truncate_cells_by_display_width():
    from urwid import calc_width

    from cc_session_control.views._rows import truncate_cells

    assert truncate_cells("short", 30) == "short"
    label = "很长的会话标题" * 6  # 42 chars = 84 cells
    out = truncate_cells(label, 30)
    assert out.endswith("…")
    assert calc_width(out, 0, len(out)) <= 30


@pytest.mark.parametrize(
    ("text", "width", "marker", "expected"),
    [
        ("unchanged", 9, "…", "unchanged"),
        ("abcdef", 5, "…", "abcd…"),
        ("你好啊", 5, "…", "你好…"),
        ("e\u0301clair", 2, "…", "e\u0301…"),
        ("👩\u200d💻abc", 3, "…", "👩\u200d💻…"),
        ("👍🏽abc", 3, "…", "👍🏽…"),
        ("🇨🇳abc", 3, "…", "🇨🇳…"),
        ("abcdef", 2, "界", "界"),
        ("abcdef", 1, "界", ""),
        ("abcdef", 1, "..", "."),
        ("abc", 0, "…", ""),
        ("abc", 1, "…", "…"),
        ("abc", 2, "…", "a…"),
    ],
)
def test_truncate_cells_preserves_terminal_clusters_and_width(
    text,
    width,
    marker,
    expected,
):
    from urwid import calc_width

    from cc_session_control.views._rows import truncate_cells

    result = truncate_cells(text, width, marker=marker)

    assert result == expected
    assert calc_width(result, 0, len(result)) <= width


def test_truncate_cells_accepts_public_width_and_marker_call_shapes():
    from cc_session_control.views._rows import truncate_cells

    assert truncate_cells("abcdef", width=2, marker="..") == ".."
    assert truncate_cells("abcdef", 1, "..") == "."


def test_session_row_label_limit_uses_terminal_cells():
    from urwid import calc_width

    label = "标" * 50
    row = SessionRow(_make_session(label=label))
    text = b"\n".join(row.render((300,), focus=False).text).decode()

    shown = "标" * 39 + "…"
    assert shown in text
    assert "标" * 40 not in text
    assert calc_width(shown, 0, len(shown)) == 79


def test_cleanup_preview_label_limit_uses_terminal_cells(monkeypatch):
    import cc_session_control.views._sessions_cleanup as cleanup_view
    from cc_session_control.data.cleanup import CleanupPlan

    monkeypatch.setattr(cleanup_view.proc, "current_determinable", lambda: True)
    session = _make_session(label="标" * 40)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(empty=[session])

    view._enter_preview("empty")

    canvas = view._body.original_widget.render((200, 40), focus=False)
    text = b"\n".join(canvas.text).decode()
    shown = "标" * 29 + "…"
    assert shown in text
    assert "标" * 30 not in text


def test_confirm_message_truncates_cjk_label_by_cells(monkeypatch):
    # A 40-CJK-char label is 80 cells — the old [:30] slice kept 60 cells of it.
    s = _make_session(sid="sid1", alive=True, current=False, pid=4242, label="标" * 40)
    app, view = _sessions_view_with(monkeypatch, s)
    view.handle_key("enter")
    msg = app._confirm_messages[-1]
    assert "标" * 14 + "…" in msg  # 28 cells + ellipsis = 29 ≤ 30
    assert "标" * 15 not in msg


def test_sessions_help_mode_r_refreshes_and_stays():
    # The footer prefix promises `r 刷新` everywhere — in help mode r must
    # refresh, not close the overlay ("其余任意键返回" excludes it).
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._show_help()
    assert view._mode == "help"
    view.handle_key("r")
    assert view._mode == "help"
    assert any("刷新" in m for m in app._notifications)
    view.handle_key("x")
    assert view._mode == "list"


def test_enter_key_live_takeover_gated_when_degraded(monkeypatch):
    # R10: like `t`/`R`, a live Enter-takeover must be refused in-TUI when
    # /proc is unavailable — not confirmed and then refused by do_resume after
    # csctl has already exited.
    import cc_session_control.views.sessions as sv_mod

    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    app, view = _sessions_view_with(monkeypatch, s)
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    view.handle_key("enter")
    assert app.result is None
    assert app._confirm_messages == []  # refused before any confirm
    assert app._notifications[-1] == sv_mod._DEGRADED


def test_enter_key_dead_session_not_gated_when_degraded(monkeypatch):
    # Resuming a DEAD session kills nothing — still allowed off /proc (B3).
    import cc_session_control.views.sessions as sv_mod

    s = _make_session(sid="sid1", alive=False, pid=None)
    app, view = _sessions_view_with(monkeypatch, s)
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    view.handle_key("enter")
    assert app.result == TmuxResumeIntent(s)


def test_t_key_takeover_gated_when_degraded(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    app, view = _sessions_view_with(monkeypatch, s)
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    view.handle_key("t")
    assert app.result is None
    assert app._notifications[-1] == sv_mod._DEGRADED


def test_status_cell_three_states():
    # Frontend-spec triple encoding: shape + word (+ color attr). The word is
    # the primary meaning carrier: 忙 = busy, 闲 = idle-alive, 停 = dead.
    from cc_session_control.views._session_row import _status_parts

    busy = _make_session(sid="b", alive=True, pid=1, status="busy")
    idle = _make_session(sid="i", alive=True, pid=1, status="idle")
    dead = _make_session(sid="d", alive=False)
    cur = _make_session(sid="c", alive=True, current=True, pid=1, status="busy")
    assert _status_parts(busy) == (" ● 忙", "status_busy")
    assert _status_parts(idle) == (" ● 闲", "alive")
    assert _status_parts(dead) == (" ○ 停", "dead")
    assert _status_parts(cur)[0].startswith("▸")


def test_status_cell_tmux_residency_badge():
    # ⧉ badge tri-state (ADR-0001): resident-alive shows it (current included),
    # bare-terminal alive doesn't, dead never — even with a stale target.
    from cc_session_control.views._session_row import _status_parts

    resident = _make_session(
        sid="r", alive=True, pid=1, status="idle", tmux_target="proj:1"
    )
    cur_resident = _make_session(
        sid="c", alive=True, current=True, pid=1, status="busy", tmux_target="proj:2"
    )
    bare = _make_session(sid="b", alive=True, pid=1, status="idle")
    dead_stale = _make_session(sid="d", alive=False, tmux_target="proj:3")
    assert _status_parts(resident) == (" ● 闲 ⧉", "alive")
    assert _status_parts(cur_resident)[0] == "▸● 忙 ⧉"
    assert "⧉" not in _status_parts(bare)[0]
    assert "⧉" not in _status_parts(dead_stale)[0]


def test_session_header_and_row_share_one_colspec():
    # Single-source column spec: header and data rows are generated from
    # SESSION_COLS, so their widths/alignments cannot drift (checklist #4).
    from cc_session_control.views._session_row import (
        _SESSION_HEADER,
        SESSION_COLS,
        SessionRow,
    )

    row = SessionRow(_make_session(sid="s1", alive=False))
    header_cols = _SESSION_HEADER.contents
    row_cols = row._w.original_widget.contents
    assert len(header_cols) == len(row_cols) == len(SESSION_COLS)
    for (_hw, ho), (_rw, ro) in zip(header_cols, row_cols, strict=True):
        assert ho == ro  # same sizing options per column


def test_key_table_handlers_resolve():
    # KEY_TABLE binds handlers by method NAME; a typo would only surface on the
    # actual keypress, so resolve every handler up front.
    from cc_session_control.views.agents import AgentsView

    for view_cls in (SessionsView, RCView, AgentsView):
        view = view_cls(FakeApp())
        for entry in view_cls.KEY_TABLE:
            handler = getattr(view, entry.handler, None)
            assert callable(handler), (view_cls.__name__, entry.handler)


def test_footer_keyhints_list_every_list_mode_key():
    # Footer, help, and dispatch are all generated from each view's KEY_TABLE
    # (single source), so this now guards content (no entry deleted), not drift.
    sessions_hints = SessionsView(FakeApp()).keyhints()
    for key in ("Enter", "t", "f", "s", "R", "d", "y", "h", "c", "/", "?"):
        assert f"{key} " in sessions_hints, f"sessions footer missing {key}"

    from cc_session_control.views.agents import AgentsView

    agents_hints = AgentsView(FakeApp()).keyhints()
    for key in ("Enter", "t", "s", "d", "w", "R", "?"):
        assert f"{key} " in agents_hints, f"agents footer missing {key}"

    rc_hints = RCView(FakeApp()).keyhints()
    for key in ("Enter", "o", "s", "a", "c", "A", "S", "?"):
        assert f"{key} " in rc_hints, f"rc footer missing {key}"


def test_key_table_bindings_match_tmux_first_dispatch():
    # ADR-0001 rebinding regression guard: key -> handler name, per tab.
    from cc_session_control.views.agents import AgentsView

    def binding(view_cls):
        return {k: e.handler for e in view_cls.KEY_TABLE for k in e.keys}

    sessions = binding(SessionsView)
    assert sessions["enter"] == "_key_resume"  # tmux 接回 (primary)
    assert sessions["t"] == "_key_terminal"  # 终端接回 (fallback)
    assert sessions["f"] == "_key_fork"  # 分叉进 tmux
    assert sessions["R"] == "_key_relaunch"  # 转后台 (no RC)

    agents = binding(AgentsView)
    assert agents["enter"] == "_takeover"  # tmux 接回
    assert agents["t"] == "_terminal"  # 终端接回
    assert "o" not in agents  # old alias dropped

    rc = binding(RCView)
    assert rc["enter"] == "_key_tmux_new"  # 新建 tmux 会话 (primary)
    assert rc["o"] == "_key_start"  # 启动远控 (demoted)
    assert "t" not in rc  # t unbound on 项目


def test_footer_hint_text_wraps_not_clips():
    # The footer trades vertical rows for width: at 80 cols the full sessions
    # key table must wrap to >1 row (urwid wrap='space'), never clip.
    import urwid

    from cc_session_control.app import FOOTER_PREFIX

    hints = SessionsView(FakeApp()).keyhints()
    text = urwid.Text(FOOTER_PREFIX + hints)
    assert text.rows((80,)) > 1


def test_rc_row_selectable():
    p = _make_project()
    row = RCRow(p)
    assert row.selectable()
    assert row.project.name == "myproj"


def test_rc_view_construct():
    app = FakeApp()
    view = RCView(app)
    assert view.widget is not None


def test_rc_row_marks_missing_directory():
    row = RCRow(_make_project(dir_exists=False, status="stopped"))
    text = _row_text(row)
    assert "✖ 缺失" in text
    assert "目录缺失" in text
    # A server still running out of a deleted dir keeps its running status.
    running = _row_text(RCRow(_make_project(dir_exists=False, status="running")))
    assert "● 运行中" in running
    assert "目录缺失" in running


def test_rc_view_missing_dir_blocks_start_keys(monkeypatch):
    import cc_session_control.views.rc as rc_view_mod
    from cc_session_control.data import rc as rc_mod

    writes = []
    monkeypatch.setattr(
        rc_view_mod.tui_actions.rc,
        "set_rc_at_startup",
        lambda directory, value: (
            writes.append((directory, value)) or _updated_setting(directory)
        ),
    )
    monkeypatch.setattr(rc_mod, "toggle_autostart", lambda name: False)

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="ghost", dir_exists=False)])

    view.handle_key("enter")  # tmux new → refused (no exit intent)
    view.handle_key("o")  # RC start → refused
    view.handle_key("c")  # would mkdir the deleted dir back — refused
    assert app.result is None
    assert not writes
    assert sum("目录缺失" in m for m in app._notifications) == 3

    view.handle_key("a")  # the removal path stays available
    assert any("开机自启" in m for m in app._notifications)


def test_sessions_view_applies_complete_refresh_batch():
    fake = [_make_session(sid="x1")]
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.apply_refresh(_refresh_batch(WorldSnapshot(sessions=fake)))

    assert view._all_sessions == fake
    assert view._cleanup_stats == {"total": 1, "empty": 0, "short": 0, "orphans": 0}
    assert view._classified == {
        "empty": 0,
        "short": 0,
        "orphan_dirs": 0,
        "zombie_procs": 0,
        "aged_entries": 0,
    }


def test_rc_view_applies_complete_refresh_batch():
    fake = [_make_project(name="p1")]
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, fake)

    assert view._projects == fake


def test_rc_view_keyhints_uses_new_labels():
    view = RCView(FakeApp())
    hints = view.keyhints()
    assert "Enter 新建会话" in hints  # tmux-first primary (ADR-0001)
    assert "o 启动远控" in hints  # RC demoted to o
    assert "开机自启" in hints
    assert "自动远控" in hints
    # batch keys are discoverable in the footer, each with its own label
    assert "A 全部启动" in hints
    assert "S 全部停止" in hints


def test_rc_view_status_bar_counts_use_new_labels():
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(
        view,
        [
            _make_project(name="p1", auto_start=True, rc_at_startup=None),
            _make_project(name="p2", auto_start=True, rc_at_startup=False),
            _make_project(name="p3", auto_start=False, rc_at_startup=False),
        ],
    )
    text = view.status.original_widget.get_text()[0]
    assert "开机自启 2" in text
    assert "自动远控关 2" in text


def test_rc_view_status_counts_per_project_setting_failures():
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(
        view,
        [
            _make_project(
                rc_at_startup_setting=RCStartupSettingRead(
                    RCStartupSettingState.MALFORMED,
                    Path("/project/.claude/settings.local.json"),
                    "bad json",
                )
            )
        ],
    )

    assert "自动远控异常 1" in view.status.original_widget.get_text()[0]


def test_rc_view_status_exposes_snapshot_ledger_warning():
    from cc_session_control.data import environments
    from cc_session_control.data.snapshot import WorldSnapshot

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    snap = WorldSnapshot(
        rc_projects=[_make_project(name="p1")],
        environment_reconciliation=environments.Reconciliation(
            ledger_history_complete=False,
            warnings=("环境台账操作失败（read）：permission denied",),
        ),
    )

    view.apply_refresh(_refresh_batch(snap))

    assert "⚠ 环境台账异常 1" in view.status.original_widget.get_text()[0]


def test_rc_view_enter_exits_with_tmux_new():
    # Enter = 新建 tmux 会话并进入 (primary); o = 启动远控 (demoted, gated).
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", directory="/tmp/p1")])

    view.handle_key("enter")

    assert app.result == TmuxNewIntent("/tmp/p1")
    assert app._submitted_actions == []


def test_rc_view_o_key_starts_rc_server(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    started = []
    monkeypatch.setattr(
        rc_mod,
        "start_one_result",
        lambda path: (
            started.append(path) or rc_mod.StartResult(rc_mod.StartState.STARTED, path)
        ),
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", status="stopped")])

    view.handle_key("o")

    assert started == ["/tmp/myproj"]  # start_one takes the PATH key
    assert app.result is None  # stays in csctl
    assert app._submitted_actions == ["project.start"]
    assert any("已启动 p1" in m for m in app._notifications)


def test_rc_view_focus_follows_project_across_reorder():
    # Activity ordering may move rows between refreshes; the cursor must stay
    # on the same project (row_key identity), not the same list position.
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    a = _make_project(name="a", directory="/tmp/a")
    b = _make_project(name="b", directory="/tmp/b")
    _apply_projects(view, [a, b])
    view.walker.set_focus(1)  # cursor on /tmp/b

    _apply_projects(view, [b, a])  # reorder (activity flip)

    focused = view.walker.get_focus()[0]
    assert focused.project.directory == "/tmp/b"  # followed identity, not index


def test_rc_view_c_key_notifies_with_new_label(monkeypatch):
    import cc_session_control.views.rc as rc_view_mod

    writes = []
    monkeypatch.setattr(
        rc_view_mod.tui_actions.rc,
        "set_rc_at_startup",
        lambda directory, value: (
            writes.append((directory, value)) or _updated_setting(directory)
        ),
    )

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", rc_at_startup=None)])

    view.handle_key("c")

    assert writes  # toggle routed through the seam, not real disk
    assert app._submitted_actions == ["project.write-settings"]
    assert any("自动远控" in m for m in app._notifications)


def test_rc_view_reports_unavailable_trust_in_status_and_start_refusal(
    monkeypatch,
):
    from cc_session_control.data import rc as rc_mod
    from cc_session_control.data.project_settings import (
        ProjectSettingsResult,
        ProjectSettingsState,
    )
    from cc_session_control.models import TrustDecision

    starts = []
    monkeypatch.setattr(
        rc_mod,
        "start_one_result",
        lambda path: starts.append(path),
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    projects = [
        _make_project(
            name="p1",
            trusted=False,
            trust_decision=TrustDecision.UNAVAILABLE,
        ),
    ]
    _apply_projects(
        view,
        projects,
        settings=ProjectSettingsResult(
            ProjectSettingsState.MALFORMED,
            {},
            "bad JSON",
        ),
    )

    assert "项目设置不可用" in view._status_text()
    view.handle_key("o")
    assert starts == []
    assert any("项目设置不可用" in item for item in app._notifications)


def test_rc_view_reports_typed_settings_write_failure(monkeypatch):
    import cc_session_control.views.rc as rc_view_mod

    def fail_write(directory, value):
        return SettingWriteResult(
            SettingWriteState.FAILED,
            Path(directory) / ".claude" / "settings.local.json",
            SettingWriteFailure.REPLACE,
            "read-only filesystem",
        )

    monkeypatch.setattr(rc_view_mod.tui_actions.rc, "set_rc_at_startup", fail_write)
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", rc_at_startup=None)])

    view.handle_key("c")

    assert any("配置写入失败（replace）" in item for item in app._notifications)


def test_rc_view_a_key_notifies_with_new_label(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    monkeypatch.setattr(rc_mod, "toggle_autostart", lambda name: True)

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1")])

    view.handle_key("a")

    assert app._submitted_actions == ["project.toggle-autostart"]
    assert any("开机自启" in m for m in app._notifications)


def test_rc_S_key_confirms_then_stops_all(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    stopped = {"n": 0}
    monkeypatch.setattr(
        rc_mod,
        "stop_all_result",
        lambda: (
            stopped.__setitem__("n", stopped["n"] + 1)
            or rc_mod.StopAllResult(rc_mod.StopState.STOPPED, "rc")
        ),
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", status="running")])

    view.handle_key("S")
    assert stopped["n"] == 0  # nothing stopped until the confirm is accepted
    assert app._confirm_messages and "停止全部" in app._confirm_messages[0]

    app._last_confirm()  # simulate pressing y
    assert stopped["n"] == 1
    assert app._submitted_actions == ["project.stop-all"]
    assert any("已停止全部" in m for m in app._notifications)


def test_rc_A_key_submits_start_all(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    monkeypatch.setattr(
        rc_mod,
        "start_all_listed_result",
        lambda: rc_mod.StartManyResult(started=2),
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1")])

    view.handle_key("A")

    assert app._submitted_actions == ["project.start-all"]
    assert app._notifications[-1] == "已启动 2 个项目"


# === Unified-keys: Sessions terminate now `s` + confirms ====================


def test_sessions_s_key_confirms_then_terminates(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    killed = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "take_over",
        lambda *_: killed.__setitem__("n", killed["n"] + 1) or "killed",
    )
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", pid=123, alive=True, current=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("s")
    assert killed["n"] == 0  # a confirm is requested, nothing killed yet
    assert app._confirm_messages and "停止" in app._confirm_messages[0]

    app._last_confirm()  # simulate pressing y
    assert killed["n"] == 1
    assert app._submitted_actions == ["session.stop"]
    assert any("已停止" in m for m in app._notifications)


def test_sessions_s_key_guards_before_confirm(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "take_over",
        lambda *_: "killed",
    )
    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("s")
    assert app._confirm_messages == []  # guard fires BEFORE any confirm
    assert any("未在运行" in m for m in app._notifications)


# === Unified confirm: takeover/relaunch live + degrade gate + f guard =======


def test_would_take_over_matches_resume_plan():
    from cc_session_control.actions.session_ops import _resume_plan, would_take_over

    live = _make_session(alive=True, current=False)
    dead = _make_session(alive=False)
    assert would_take_over(live) is _resume_plan(live)[2] is True
    assert would_take_over(dead) is _resume_plan(dead)[2] is False
    # fork is a copy — never a takeover.
    assert would_take_over(live, fork=True) is False


def test_sessions_enter_live_confirms_takeover(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", alive=True, current=False, pid=999)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("enter")
    assert app.result is None  # not resumed until confirmed
    assert app._confirm_messages and "接回会话" in app._confirm_messages[0]
    assert "终止原进程" in app._confirm_messages[0]

    app._last_confirm()
    assert isinstance(app.result, TmuxResumeIntent)  # tmux-first primary


def test_sessions_enter_dead_resumes_directly():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False, current=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("enter")
    assert app._confirm_messages == []  # dead: no takeover, no confirm
    assert app.result == TmuxResumeIntent(view._all_sessions[0])


def test_sessions_R_live_confirms_relaunch(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    relaunched = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "do_tmux_resume",
        lambda s: relaunched.__setitem__("n", relaunched["n"] + 1) or "proj:1",
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", alive=True, current=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("R")
    assert relaunched["n"] == 0
    assert app._confirm_messages and "转入后台" in app._confirm_messages[0]

    app._last_confirm()
    assert relaunched["n"] == 1
    assert app.result is None  # 转后台 stays in csctl (no exit intent)
    assert app._submitted_actions == ["session.background"]
    assert any("已转入后台" in m and "proj:1" in m for m in app._notifications)


def test_sessions_R_refuses_resident_session(monkeypatch):
    # A tmux-resident session needs no backgrounding: notify, no confirm, no spawn.
    import cc_session_control.views.sessions as sv_mod

    spawned = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "do_tmux_resume",
        lambda s: spawned.__setitem__("n", spawned["n"] + 1) or "proj:1",
    )
    s = _make_session(sid="res", alive=True, current=False, pid=1, tmux_target="proj:9")
    app, view = _sessions_view_with(monkeypatch, s)

    view.handle_key("R")
    assert spawned["n"] == 0
    assert app._confirm_messages == []
    assert "已在 tmux" in app._notifications[-1]


def test_sessions_R_degraded_still_relaunches_dead(monkeypatch):
    # B3: relaunching a DEAD session kills nothing — must NOT be blocked off /proc.
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    relaunched = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "do_tmux_resume",
        lambda s: relaunched.__setitem__("n", relaunched["n"] + 1) or "proj:1",
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False, current=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("R")
    assert relaunched["n"] == 1  # dead relaunch is not gated by degrade


def test_sessions_R_degraded_refuses_live_takeover(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    relaunched = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "do_tmux_resume",
        lambda s: relaunched.__setitem__("n", relaunched["n"] + 1) or "proj:1",
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="live", alive=True, current=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("R")
    assert relaunched["n"] == 0
    assert app._confirm_messages == []
    assert any("降级" in m for m in app._notifications)


def test_sessions_y_copies_through_action_runner(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    copied = []
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "to_clipboard",
        lambda command: copied.append(command) or True,
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("y")

    assert app._submitted_actions == ["session.copy-command"]
    assert copied and "--resume dead" in copied[0]


def test_sessions_f_refuses_current():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="cur", alive=True, current=True)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("f")
    assert app.result is None
    assert any("不能分叉当前会话" in m for m in app._notifications)


def test_sessions_f_forks_into_tmux_no_confirm(monkeypatch):
    # f = 分叉进 tmux: a fork is a copy — no kill, no confirm, even for a live
    # RESIDENT session (it must spawn its own window, never attach in place).
    s = _make_session(
        sid="live", alive=True, current=False, pid=999, tmux_target="proj:4"
    )
    app, view = _sessions_view_with(monkeypatch, s)

    view.handle_key("f")
    assert app._confirm_messages == []
    assert app.result == TmuxResumeIntent(s, fork=True)


def test_rc_s_running_confirms_stop(monkeypatch):
    from cc_session_control.data import rc as rc_mod

    stopped = {"n": 0}
    monkeypatch.setattr(
        rc_mod,
        "stop_one_result",
        lambda path: (
            stopped.__setitem__("n", stopped["n"] + 1)
            or rc_mod.StopResult(rc_mod.StopState.STOPPED, path)
        ),
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", status="running")])

    view.handle_key("s")
    assert stopped["n"] == 0
    assert app._confirm_messages and "停止远控服务" in app._confirm_messages[0]

    app._last_confirm()
    assert stopped["n"] == 1
    assert app._submitted_actions == ["project.stop"]


def test_rc_s_not_running_no_confirm():
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", status="stopped")])

    view.handle_key("s")
    assert app._confirm_messages == []
    assert any("未在运行" in m for m in app._notifications)


# === Phase 7: D9 session badges + hide-filter union =========================


def test_session_row_renders_source_and_flag_badges():
    row = SessionRow(
        _make_session(source="cli", rc_exposed=True, agent_short="abcd1234")
    )
    text = _row_text(row)
    assert "CLI" in text  # source badge
    assert "📱" in text  # RC-exposure marker (phone; Emoji_Presentation, width-stable)
    # Agent-link is intentionally NOT a row marker anymore: orthogonal to 远控,
    # already covered by the 来源 BG badge + the 后台 tab. Lock that it is gone.
    assert "代" not in text
    assert "⚙" not in text


def test_session_row_source_badge_maps_vscode_to_ide():
    text = _row_text(SessionRow(_make_session(source="vscode")))
    assert "IDE" in text


def test_hide_filter_unions_source_sdk(monkeypatch):
    # A session flagged sdk via the REGISTRY source (not a transcript `hidden`
    # tag) must still be hidden by the `h` toggle (D9 union via bridge_or_sdk).
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [
        _make_session(sid="normal", source="cli", hidden=set()),
        _make_session(sid="sdkreg", source="sdk", hidden=set()),
    ]
    view._apply_filter()
    assert [s.sid for s in view._sessions] == ["normal", "sdkreg"]

    view._show_hidden = False
    view._apply_filter()
    assert [s.sid for s in view._sessions] == ["normal"]


def test_sessions_view_applies_snapshot_and_plan_from_same_batch():
    plan = CleanupPlan()

    fake = [_make_session(sid="snap1")]
    from cc_session_control.models import SessionProc

    snap = WorldSnapshot(
        sessions=fake, session_procs=[SessionProc(pid=9, sid="snap1")], cur={42}
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.apply_refresh(_refresh_batch(snap, plan=plan))

    assert view._all_sessions == fake
    assert view._plan is plan


# === Phase 7: RC tri-state + spawn_mode + servers + env ledger ==============


def test_rc_row_rc_at_startup_tristate():
    assert "未设置" in _row_text(RCRow(_make_project(rc_at_startup=None)))
    assert "开" in _row_text(RCRow(_make_project(rc_at_startup=True)))
    assert "关" in _row_text(RCRow(_make_project(rc_at_startup=False)))


def test_rc_row_distinguishes_setting_read_failure_from_unset():
    row = RCRow(
        _make_project(
            rc_at_startup_setting=RCStartupSettingRead(
                RCStartupSettingState.INVALID,
                Path("/project/.claude/settings.local.json"),
                "not a boolean",
            )
        )
    )

    text = _row_text(row)
    assert "读取失败" in text
    assert "未设置" not in text


def test_rc_row_shows_spawn_mode():
    assert "same-dir" in _row_text(RCRow(_make_project(spawn_mode="same-dir")))


def test_server_row_managed_external_badge():
    managed = _row_text(
        ServerRow(RCServer(name="ws/a", managed=True, pid=1, status="running"))
    )
    external = _row_text(
        ServerRow(RCServer(name="ws/b", managed=False, pid=2, status="running"))
    )
    assert "托管" in managed
    assert "外部" in external


def test_rc_view_renders_servers_but_no_env_ledger():
    # The env ledger is deliberately NOT rendered in the TUI (csctl can't act on
    # cloud environments) — only project rows + the RC server section remain.
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(
        view,
        [_make_project(name="p1")],
        servers=[
            RCServer(name="ws/ext", managed=False, pid=7, status="running"),
        ],
    )

    blob = "\n".join(_row_text(view.walker[i]) for i in range(len(view.walker)))
    assert "外部" in blob  # external server badge still shown
    assert "环境台账" not in blob  # env ledger section gone
    assert "云端需手动删除" not in blob


def test_rc_view_applies_servers_from_snapshot():
    snap = WorldSnapshot(
        rc_projects=[_make_project(name="p1")],
        rc_servers=[RCServer(name="ws/x", managed=True, pid=3, status="running")],
        observed_envs=[],
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view.apply_refresh(_refresh_batch(snap))

    assert view._projects[0].name == "p1"
    assert view._servers[0].name == "ws/x"


def test_rc_view_server_rows_are_read_only(monkeypatch):
    # External servers must NOT be actionable (no takeover/restart key).
    # Focusing such a row makes every key a no-op (AC9 red line).
    import cc_session_control.views.rc as rc_view_mod

    started = {"n": 0}
    monkeypatch.setattr(
        rc_view_mod.tui_actions,
        "start_project",
        lambda *_: started.__setitem__("n", started["n"] + 1) or True,
    )
    monkeypatch.setattr(
        rc_view_mod.tui_actions,
        "stop_project",
        lambda *_: started.__setitem__("n", started["n"] + 1) or True,
    )

    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._projects = []
    view._servers = [RCServer(name="ws/ext", managed=False, pid=9, status="running")]
    view._rebuild()

    # Focus the external ServerRow explicitly.
    for i in range(len(view.walker)):
        if isinstance(view.walker[i], ServerRow):
            view.walker.set_focus(i)
            break
    assert view._selected() is None  # not an RCProject -> nothing actionable

    for key in ("enter", "s", "a", "c"):
        view.handle_key(key)
    assert started["n"] == 0
    assert app._notifications == []


# === AC9: red-line grep/AST assertions ======================================

_SRC_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "src",
    "cc_session_control",
)


def _iter_src_files():
    for root, _dirs, files in os.walk(_SRC_ROOT):
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_no_deregister_or_delete_env_symbols_in_src():
    # AC9: no SYMBOL named deregister/delete_env may be defined, assigned, or
    # called anywhere in src (docstring prose mentioning the word is fine — this
    # walks the AST, not the text).
    forbidden = {"deregister", "delete_env"}
    offenders = []
    for path in _iter_src_files():
        with open(path) as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in forbidden
            ):
                offenders.append((path, node.name))
            elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append((path, node.attr))
            elif isinstance(node, ast.Name) and node.id in forbidden:
                offenders.append((path, node.id))
    assert offenders == []


def test_environments_and_agent_ops_do_not_export_deregister():
    from cc_session_control.actions import agent_ops
    from cc_session_control.data import environments

    for mod in (environments, agent_ops):
        assert not hasattr(mod, "deregister")
        assert not hasattr(mod, "delete_env")


# === Post-review fix B: RC honesty (env ledger is CLI-only now) ==============


def test_rc_view_help_points_ledger_queries_at_cli():
    # The env ledger left the TUI; the help must still be honest about WHY
    # (csctl cannot deregister cloud envs) and point at `csctl env`.
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    view._show_help()
    canvas = view._body.original_widget.render((100, 40), focus=False)
    blob = b"\n".join(canvas.text).decode()
    assert "无法注销" in blob
    assert "csctl env" in blob


def test_rc_view_help_is_overlay_and_keeps_list(monkeypatch):
    # Help is an Overlay over the intact project list (like Sessions/Agents) —
    # the old walker-replacing rows were unscrollable on short terminals.
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1")])
    rows_before = len(view.walker)

    view._show_help()
    assert isinstance(view._body.original_widget, urwid.Overlay)
    assert len(view.walker) == rows_before  # list untouched underneath

    view.handle_key("r")  # r refreshes, stays in help
    assert view._help is True
    assert isinstance(view._body.original_widget, urwid.Overlay)

    view.handle_key("x")  # any non-global key returns
    assert view._body.original_widget is view._list_body
    assert view._help is False


def test_rc_view_c_key_full_tristate_cycle(monkeypatch):
    # Fix 5: cycle must be None→True→False→None so explicit True is reachable.
    import cc_session_control.views.rc as rc_view_mod

    writes = []
    monkeypatch.setattr(
        rc_view_mod.tui_actions.rc,
        "set_rc_at_startup",
        lambda directory, value: writes.append(value) or _updated_setting(directory),
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]

    for start, expected in ((None, True), (True, False), (False, None)):
        _apply_projects(view, [_make_project(name="p", rc_at_startup=start)])
        view.handle_key("c")
        assert writes[-1] is expected


def test_rc_view_c_key_refuses_unavailable_setting_evidence(monkeypatch):
    import cc_session_control.views.rc as rc_view_mod

    writes = []
    monkeypatch.setattr(
        rc_view_mod.tui_actions.rc,
        "set_rc_at_startup",
        lambda directory, value: writes.append(value),
    )
    source = Path("/project/.claude/settings.local.json")
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(
        view,
        [
            _make_project(
                rc_at_startup_setting=RCStartupSettingRead(
                    RCStartupSettingState.MALFORMED,
                    source,
                    "bad json",
                )
            )
        ],
    )

    view.handle_key("c")

    assert writes == []
    assert app._submitted_actions == []
    assert any(
        "malformed" in message
        and str(source) in message
        and "bad json" in message
        and "不写入配置" in message
        for message in app._notifications
    )


# === Post-review fix B: Sessions degraded honesty + cleanup parity ==========


def _focus_dead_session(view, **overrides):
    overrides.setdefault("alive", False)
    view._all_sessions = [_make_session(**overrides)]
    view._apply_filter()
    view._rebuild()
    view.walker.set_focus(0)


def test_delete_honest_feedback_true_then_false(monkeypatch):
    # Fix 3 / L4: only claim 已删除 when remove_session truly removed something.
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    _focus_dead_session(view)

    from cc_session_control.data.removal import CleanupExecution

    removed = CleanupExecution(completed=["sid"])
    monkeypatch.setattr(sv_mod.tui_actions.cleanup, "remove_session", lambda s: removed)
    view.handle_key("d")
    assert app._submitted_actions == ["session.delete"]
    assert app._notifications[-1] == "已删除"

    monkeypatch.setattr(
        sv_mod.tui_actions.cleanup,
        "remove_session",
        lambda s: CleanupExecution(),
    )
    view.handle_key("d")
    assert app._notifications[-1] == "无可删除内容"


def test_delete_failure_does_not_claim_success(monkeypatch, tmp_path):
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.data.removal import (
        CleanupExecution,
        PathRemoval,
        RemovalStatus,
    )

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    failed = CleanupExecution(
        removals=[PathRemoval(tmp_path / "locked", RemovalStatus.FAILED, "denied")]
    )
    monkeypatch.setattr(sv_mod.tui_actions.cleanup, "remove_session", lambda s: failed)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    _focus_dead_session(view)

    view.handle_key("d")

    assert "失败" in app._notifications[-1]
    assert "已删除" not in app._notifications[-1]


def test_delete_partial_failure_mentions_removed_path(monkeypatch, tmp_path):
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.data.removal import (
        CleanupExecution,
        PathRemoval,
        RemovalStatus,
    )

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: True)
    partial = CleanupExecution(
        removals=[
            PathRemoval(tmp_path / "gone", RemovalStatus.REMOVED),
            PathRemoval(tmp_path / "locked", RemovalStatus.FAILED, "denied"),
        ]
    )
    monkeypatch.setattr(sv_mod.tui_actions.cleanup, "remove_session", lambda s: partial)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    _focus_dead_session(view)

    view.handle_key("d")

    notice = app._notifications[-1]
    assert "部分失败" in notice
    assert "已删除路径 1" in notice


def test_delete_refuses_when_current_undeterminable(monkeypatch):
    # Fix 2a / R10: no /proc -> the delete must refuse honestly, not "delete".
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    removed = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.cleanup,
        "remove_session",
        lambda s: removed.__setitem__("n", removed["n"] + 1),
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    _focus_dead_session(view)

    view.handle_key("d")
    assert removed["n"] == 0
    assert app._notifications[-1] == sv_mod._DEGRADED


def test_cleanup_preview_refuses_when_undeterminable_not_nothing(monkeypatch):
    # Fix 2a: a degraded refusal must NOT read as "无…需要清理".
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._enter_preview("empty")
    assert view._mode == "list"  # never opened a preview
    assert app._notifications[-1] == sv_mod._DEGRADED
    assert "需要清理" not in app._notifications[-1]


def test_cleanup_submenu_exposes_zombie_and_aged_actions(monkeypatch):
    # Fix 4: CLI/TUI parity — the submenu offers the pid-keyed zombie sweep and
    # the age sweep, with counts from the plan-derived classified dict.
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._classified = {
        "empty": 1,
        "short": 2,
        "orphan_dirs": 3,
        "zombie_procs": 4,
        "aged_entries": 5,
    }
    view._enter_cleanup()
    keys = [w.action_key for w in view._cleanup_walker]
    assert keys == ["empty", "short", "orphans", "zombies", "aged"]
    blob = "\n".join(_row_text(w) for w in view._cleanup_walker)
    assert "4" in blob and "5" in blob  # zombie + aged counts surfaced


def test_zombie_sweep_preview_and_confirm(monkeypatch):
    # Fix 4: zombie sweep previews the frozen plan's dead pid files and confirm
    # routes the SAME frozen targets to the revalidating executor.
    import cc_session_control.views._sessions_cleanup as cl_mod
    from cc_session_control.data.cleanup import CleanupPlan

    monkeypatch.setattr(cl_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(zombie_pids=[111])

    view._enter_preview("zombies")
    assert view._mode == "preview"
    assert view._preview_action.key == "zombies"

    import dataclasses

    swept = {}
    from cc_session_control.data.removal import CleanupExecution

    view._preview_action = dataclasses.replace(
        view._preview_action,
        execute=lambda plan, pids, **k: (
            swept.update(pids=pids) or CleanupExecution(completed=["111"])
        ),
    )
    view._confirm_cleanup()
    assert swept["pids"] == [111]  # exactly the previewed targets
    assert app._submitted_actions == ["session.cleanup.zombies"]
    assert any("僵尸会话文件" in m for m in app._notifications)


def test_zombie_sweep_gated_when_undeterminable(monkeypatch):
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.data.cleanup import CleanupPlan

    monkeypatch.setattr(sv_mod.proc, "current_determinable", lambda: False)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(zombie_pids=[111])
    view._enter_preview("zombies")
    assert view._mode == "list"
    assert app._notifications[-1] == sv_mod._DEGRADED


def test_aged_sweep_preview_and_confirm_not_gated(monkeypatch):
    # Fix 4: the age sweep is mtime-only -> NOT R10-gated; works even with no /proc.
    import cc_session_control.views._sessions_cleanup as cl_mod
    from cc_session_control.data.cleanup import CleanupPlan

    monkeypatch.setattr(cl_mod.proc, "current_determinable", lambda: False)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(aged_entries=["shell-snapshots/old.sh"])

    view._enter_preview("aged")
    assert view._mode == "preview"
    assert view._preview_action.key == "aged"

    import dataclasses

    swept = {}
    from cc_session_control.data.removal import CleanupExecution

    view._preview_action = dataclasses.replace(
        view._preview_action,
        execute=lambda plan, entries, **k: (
            swept.update(entries=entries)
            or CleanupExecution(completed=["shell-snapshots/old.sh"])
        ),
    )
    view._confirm_cleanup()
    assert swept["entries"] == ["shell-snapshots/old.sh"]
    assert any("过期项" in m for m in app._notifications)


def test_cleanup_confirmation_reports_partial_failure(monkeypatch, tmp_path):
    import dataclasses

    import cc_session_control.views._sessions_cleanup as cl_mod
    from cc_session_control.data.cleanup import CleanupPlan
    from cc_session_control.data.removal import (
        CleanupExecution,
        PathRemoval,
        RemovalStatus,
    )

    monkeypatch.setattr(cl_mod.proc, "current_determinable", lambda: True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(zombie_pids=[111, 222])
    view._enter_preview("zombies")
    partial = CleanupExecution(
        completed=["111"],
        removals=[
            PathRemoval(tmp_path / "111.json", RemovalStatus.REMOVED),
            PathRemoval(
                tmp_path / "222.json", RemovalStatus.FAILED, "permission denied"
            ),
        ],
    )
    view._preview_action = dataclasses.replace(
        view._preview_action, execute=lambda plan, targets: partial
    )

    view._confirm_cleanup()

    notice = app._notifications[-1]
    assert "部分完成" in notice
    assert "失败 1" in notice
    assert "已清理 2" not in notice


def test_cleanup_menu_surfaces_partial_plan_warning():
    from cc_session_control.data.cleanup import CleanupIssue, CleanupPlan

    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(issues=[CleanupIssue("orphan_dirs", "permission denied")])

    view._enter_cleanup()

    assert "预览不完整" in app._notifications[-1]
    assert "permission denied" in app._notifications[-1]
