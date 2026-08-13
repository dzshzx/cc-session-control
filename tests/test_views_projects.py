"""View unit tests: the slim projects view — ProjectRow rendering, launcher
gates, curation/status-bar surface, and the removal of every RC verb."""

import urwid
from view_helpers import (
    FakeApp,
    _apply_projects,
    _make_project,
    _row_text,
)

from cc_session_control.actions.session_ops import TmuxNewIntent
from cc_session_control.views.projects import ProjectRow, ProjectsView


def test_project_row_selectable():
    p = _make_project()
    row = ProjectRow(p)
    assert row.selectable()
    assert row.project.name == "myproj"


def test_projects_view_construct():
    app = FakeApp()
    view = ProjectsView(app)
    assert view.widget is not None


def test_project_row_marks_missing_directory_in_directory_column():
    text = _row_text(ProjectRow(_make_project(dir_exists=False)))
    assert "目录缺失" in text
    assert "myproj" in text


def test_projects_view_missing_dir_blocks_launch_keys():
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="ghost", dir_exists=False)])

    view.handle_key("enter")  # chooser → refused (no exit intent, no overlay)
    view.handle_key("x")
    view.handle_key("k")
    assert app.result is None
    assert not isinstance(view._body.original_widget, urwid.Overlay)
    assert sum("目录缺失" in m for m in app._notifications) == 3


def test_projects_view_applies_complete_refresh_batch():
    fake = [_make_project(name="p1")]
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(view, fake)

    assert view._projects == fake


def test_projects_view_keyhints_use_launcher_and_curation_labels():
    hints = ProjectsView(FakeApp()).keyhints()
    assert "Enter 新建会话" in hints  # tmux-first primary (ADR-0001)
    assert "x 新codex" in hints
    assert "k 新kimi" in hints
    assert "p 钉选" in hints
    assert "h 隐藏" in hints
    assert "H 显隐藏" in hints


def test_projects_view_has_no_rc_surface_left():
    # The Remote Control verbs/columns are gone for good: no o/s/c/S bindings,
    # no 远控 labels anywhere in the footer or the help overlay.
    from cc_session_control.views._keytable import help_lines

    view = ProjectsView(FakeApp())
    bound = {k for e in view.KEY_TABLE for k in e.keys}
    assert bound.isdisjoint({"o", "s", "c", "S"})
    hints = view.keyhints()
    assert "远控" not in hints
    blob = "\n".join(help_lines(view.KEY_TABLE, view.HELP_LAYOUT))
    assert "远控" not in blob

    # The retired keys are inert: no notification, no submitted action.
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(view, [_make_project()])
    for key in ("o", "s", "c", "S"):
        view.handle_key(key)
    assert app._notifications == []
    assert app._submitted_actions == []


def test_projects_view_status_bar_counts():
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(
        view,
        [
            _make_project(name="p1", directory="/tmp/p1"),
            _make_project(name="p2", directory="/tmp/p2", dir_exists=False),
            _make_project(name="p3", directory="/tmp/p3", hidden=True),
        ],
    )
    text = view.status.original_widget.get_text()[0]
    assert "共 2 项目" in text
    assert "目录缺失 1" in text
    assert "已隐藏 1" in text


def test_projects_view_enter_exits_with_tmux_new():
    # Enter = CLI 选择器 → 新建 tmux 会话并进入 (primary; the conftest pins
    # claude as the only active provider, so the chooser's default first row
    # is claude and Enter-Enter ≡ the pre-chooser direct launch).
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(name="p1", directory="/tmp/p1")])

    view.handle_key("enter")  # opens the chooser
    assert app.result is None
    view.handle_key("enter")  # confirms the default claude row

    assert app.result == TmuxNewIntent("/tmp/p1")
    assert app._submitted_actions == []


def test_projects_view_empty_placeholder():
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(view, [])

    assert "暂无项目" in _row_text(view.walker[0])


def test_projects_view_help_overlay_opens_and_closes():
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(view, [_make_project()])

    view.handle_key("?")
    assert isinstance(view._body.original_widget, urwid.Overlay)
    assert "其余任意键返回" in view.keyhints()

    view.handle_key("esc")  # any key returns to the list
    assert view._body.original_widget is view._list_body
