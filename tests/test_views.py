"""View unit tests: Sessions-view core — construct widgets, filter/hidden toggle, resume key handling (t/enter/R/f/y), status cell, and cross-tab key-table/footer infra shared with RC/Agents."""

import pytest
import urwid
from view_helpers import (
    FakeApp,
    _make_session,
    _refresh_batch,
    _row_text,
    _set_proc_complete,
)

from cc_session_control.actions.session_ops import (
    AttachIntent,
    ResumeIntent,
    TmuxResumeIntent,
)
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.views._confirm import DEGRADED
from cc_session_control.views.rc import RCView
from cc_session_control.views.sessions import SessionRow, SessionsView


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
    monkeypatch.setattr(
        sv_mod.proc,
        "probe_current_ancestors",
        lambda: sv_mod.proc.AncestorProbe(frozenset({999})),
    )
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
    _set_proc_complete(monkeypatch, sv_mod.proc, False)
    view.handle_key("enter")
    assert app.result is None
    assert app._confirm_messages == []  # refused before any confirm
    assert app._notifications[-1] == DEGRADED


def test_enter_key_dead_session_not_gated_when_degraded(monkeypatch):
    # Resuming a DEAD session kills nothing — still allowed off /proc (B3).
    import cc_session_control.views.sessions as sv_mod

    s = _make_session(sid="sid1", alive=False, pid=None)
    app, view = _sessions_view_with(monkeypatch, s)
    _set_proc_complete(monkeypatch, sv_mod.proc, False)
    view.handle_key("enter")
    assert app.result == TmuxResumeIntent(s)


def test_t_key_takeover_gated_when_degraded(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    app, view = _sessions_view_with(monkeypatch, s)
    _set_proc_complete(monkeypatch, sv_mod.proc, False)
    view.handle_key("t")
    assert app.result is None
    assert app._notifications[-1] == DEGRADED


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

    from cc_session_control.app import FOOTER_PREFIX

    hints = SessionsView(FakeApp()).keyhints()
    text = urwid.Text(FOOTER_PREFIX + hints)
    assert text.rows((80,)) > 1


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


def test_two_sessions_views_own_independent_lists_from_one_batch():
    batch = _refresh_batch(
        WorldSnapshot(
            sessions=[
                _make_session(sid="one"),
                _make_session(sid="two"),
            ]
        )
    )
    first = SessionsView(FakeApp())
    second = SessionsView(FakeApp())

    first.apply_refresh(batch)
    second.apply_refresh(batch)
    removed = first._all_sessions.pop()
    first._apply_filter()

    assert removed.sid == "two"
    assert [session.sid for session in first._sessions] == ["one"]
    assert [session.sid for session in second._sessions] == ["one", "two"]
    assert [session.sid for session in batch.snapshot.sessions] == ["one", "two"]


def test_sessions_s_key_confirms_then_terminates(monkeypatch):
    import cc_session_control.views.sessions as sv_mod

    killed = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "take_over_result",
        lambda *_: (
            killed.__setitem__("n", killed["n"] + 1)
            or sv_mod.tui_actions.session_ops.TakeOverOutcome(
                sv_mod.tui_actions.session_ops.TakeOverState.KILLED,
            )
        ),
    )
    _set_proc_complete(monkeypatch, sv_mod.proc, True)
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
    _set_proc_complete(monkeypatch, sv_mod.proc, True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._all_sessions = [_make_session(sid="dead", alive=False)]
    view._apply_filter()
    view._rebuild()

    view.handle_key("s")
    assert app._confirm_messages == []  # guard fires BEFORE any confirm
    assert any("未在运行" in m for m in app._notifications)


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

    monkeypatch.setattr(
        sv_mod.proc,
        "probe_current_ancestors",
        lambda: sv_mod.proc.AncestorProbe(frozenset({999})),
    )
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

    monkeypatch.setattr(
        sv_mod.proc,
        "probe_current_ancestors",
        lambda: sv_mod.proc.AncestorProbe(frozenset({999})),
    )
    relaunched = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "do_tmux_resume_result",
        lambda s: (
            relaunched.__setitem__("n", relaunched["n"] + 1)
            or sv_mod.tui_actions.session_ops.TmuxResumeOutcome("proj:1")
        ),
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
        "do_tmux_resume_result",
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

    _set_proc_complete(monkeypatch, sv_mod.proc, False)
    relaunched = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "do_tmux_resume_result",
        lambda s: (
            relaunched.__setitem__("n", relaunched["n"] + 1)
            or sv_mod.tui_actions.session_ops.TmuxResumeOutcome("proj:1")
        ),
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

    _set_proc_complete(monkeypatch, sv_mod.proc, False)
    relaunched = {"n": 0}
    monkeypatch.setattr(
        sv_mod.tui_actions.session_ops,
        "do_tmux_resume_result",
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
