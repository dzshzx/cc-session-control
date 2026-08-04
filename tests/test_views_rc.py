"""View unit tests: RC/projects-view — RCRow/RCView rendering, project actions (o/c/S/s), server rows, and the no-deregister capability guard."""

import ast
import os
from pathlib import Path

import urwid
from view_helpers import (
    FakeApp,
    _apply_projects,
    _make_project,
    _refresh_batch,
    _row_text,
    _updated_setting,
)

from cc_session_control.actions.session_ops import (
    TmuxNewIntent,
)
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
    SettingWriteFailure,
    SettingWriteResult,
    SettingWriteState,
)
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import (
    InventoryIssue,
    RCServer,
    RCStartupSettingRead,
    RCStartupSettingState,
)
from cc_session_control.views.rc import RCRow, RCView, ServerRow


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

    writes = []
    monkeypatch.setattr(
        rc_view_mod.tui_actions,
        "write_rc_at_startup",
        lambda directory, value: (
            writes.append((directory, value)) or _updated_setting(directory)
        ),
    )

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
    assert "自动远控" in hints
    # batch keys are discoverable in the footer, each with its own label
    assert "S 全部停止" in hints


def test_rc_view_status_bar_counts_use_new_labels():
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(
        view,
        [
            _make_project(name="p1", rc_at_startup=None),
            _make_project(name="p2", rc_at_startup=False),
            _make_project(name="p3", rc_at_startup=False),
        ],
    )
    text = view.status.original_widget.get_text()[0]
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


def test_rc_view_shows_unknown_inventory_status_and_warning():
    issue = InventoryIssue(
        "tmux list-windows",
        None,
        "lost server connection",
    )
    project = _make_project(status="unknown")
    snap = WorldSnapshot(
        rc_projects=[project],
        rc_inventory_issues=(issue,),
    )
    app = FakeApp()
    view = RCView(app)
    app.views = [view]

    view.apply_refresh(_refresh_batch(snap))

    assert "未知" in _row_text(view.walker[0])
    assert "⚠ RC 清单不完整 1" in view.status.original_widget.get_text()[0]

    view.handle_key("o")
    view.handle_key("s")
    assert app._submitted_actions == []
    assert app._notifications == [
        "RC 清单不可用 — 已拒绝启动",
        "RC 清单不可用 — 无法确认是否运行",
    ]


def test_rc_view_enter_exits_with_tmux_new():
    # Enter = CLI 选择器 → 新建 tmux 会话并进入 (primary; the conftest pins
    # claude as the only active provider, so the chooser's default first row
    # is claude and Enter-Enter ≡ the pre-chooser direct launch);
    # o = 启动远控 (demoted, gated).
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", directory="/tmp/p1")])

    view.handle_key("enter")  # opens the chooser
    assert app.result is None
    view.handle_key("enter")  # confirms the default claude row

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
        rc_view_mod.tui_actions,
        "write_rc_at_startup",
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

    monkeypatch.setattr(rc_view_mod.tui_actions, "write_rc_at_startup", fail_write)
    app = FakeApp()
    view = RCView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", rc_at_startup=None)])

    view.handle_key("c")

    assert any("配置写入失败（replace）" in item for item in app._notifications)


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
    # No env-ledger section exists (the pipeline was removed in 0.8; csctl
    # can't act on cloud environments) — only project rows + RC servers remain.
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

    for key in ("enter", "s", "c"):
        view.handle_key(key)
    assert started["n"] == 0
    assert app._notifications == []


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


def test_agent_ops_does_not_export_deregister():
    from cc_session_control.actions import agent_ops

    assert not hasattr(agent_ops, "deregister")
    assert not hasattr(agent_ops, "delete_env")


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
        rc_view_mod.tui_actions,
        "write_rc_at_startup",
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
        rc_view_mod.tui_actions,
        "write_rc_at_startup",
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
