"""View unit tests: ADR-0007 membership provenance — the pin/hide curation
verbs, the show-hidden mode, and the status-bar counts."""

from view_helpers import (
    FakeApp,
    _apply_projects,
    _make_project,
    _refresh_batch,
)

from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import InventoryIssue
from cc_session_control.views.projects import ProjectRow, ProjectsView


def test_hidden_rows_stay_invisible_until_show_hidden_mode():
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(
        view,
        [
            _make_project(name="shown", directory="/tmp/shown"),
            _make_project(name="gone", directory="/tmp/gone", hidden=True),
        ],
    )

    directories = [
        w.project.directory for w in view.walker if isinstance(w, ProjectRow)
    ]
    assert directories == ["/tmp/shown"]

    view.handle_key("H")
    directories = [
        w.project.directory for w in view.walker if isinstance(w, ProjectRow)
    ]
    assert directories == ["/tmp/shown", "/tmp/gone"]

    view.handle_key("H")
    directories = [
        w.project.directory for w in view.walker if isinstance(w, ProjectRow)
    ]
    assert directories == ["/tmp/shown"]


def test_pin_and_hide_keys_submit_curation_actions(monkeypatch, tmp_path):
    import cc_session_control.views.projects as rc_view_mod

    writes = []
    monkeypatch.setattr(
        rc_view_mod.tui_actions,
        "toggle_project_pin",
        lambda path, name, pinned: (
            writes.append(("pin", path, pinned))
            or rc_view_mod.tui_actions.ActionResult("ok")
        ),
    )
    monkeypatch.setattr(
        rc_view_mod.tui_actions,
        "toggle_project_hidden",
        lambda path, name, hidden: (
            writes.append(("hide", path, hidden))
            or rc_view_mod.tui_actions.ActionResult("ok")
        ),
    )

    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(view, [_make_project(directory="/tmp/proj")])

    view.handle_key("p")
    view.handle_key("h")

    assert writes == [("pin", "/tmp/proj", True), ("hide", "/tmp/proj", True)]
    assert app._submitted_actions == ["project.pin", "project.hide"]


def test_hide_verb_on_a_hidden_row_unhides(monkeypatch):
    import cc_session_control.views.projects as rc_view_mod

    writes = []
    monkeypatch.setattr(
        rc_view_mod.tui_actions,
        "toggle_project_hidden",
        lambda path, name, hidden: (
            writes.append((path, hidden)) or rc_view_mod.tui_actions.ActionResult("ok")
        ),
    )

    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    _apply_projects(
        view,
        [_make_project(directory="/tmp/gone", hidden=True)],
    )
    view.handle_key("H")  # reveal the hidden row, then unhide it
    view.handle_key("h")

    assert writes == [("/tmp/gone", False)]


def test_status_bar_reports_hidden_count_and_membership_issues():
    app = FakeApp()
    view = ProjectsView(app)
    app.views = [view]
    projects = [
        _make_project(name="shown", directory="/tmp/shown"),
        _make_project(name="gone", directory="/tmp/gone", hidden=True),
    ]
    snapshot = WorldSnapshot(
        projects=projects,
        membership_issues=(
            InventoryIssue("codex trust", "/home/u/.codex/config.toml", "bad TOML"),
        ),
    )
    view.apply_refresh(_refresh_batch(snapshot, ordered_projects=tuple(projects)))

    text = view.status.original_widget.get_text()[0]
    assert "共 1 项目" in text
    assert "已隐藏 1" in text
    assert "⚠ 项目来源异常 1" in text

    view.handle_key("H")
    text = view.status.original_widget.get_text()[0]
    assert "已隐藏 1（列出中）" in text
