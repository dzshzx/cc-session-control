"""Shared plain helpers (FakeApp stub + builders) for the split `test_views*` modules."""

import urwid
from factories import make_session

from cc_session_control.actions.runner import Accepted
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.refresh import RefreshBatch
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import Project
from cc_session_control.views.projects import ProjectsView


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
        return Accepted()

    def submit_completion(self, action_key, action, on_complete):
        on_complete(action())
        return Accepted()

    def refresh_with_notice(self):
        self.trigger_async_refresh()
        self.notify("刷新中…")

    def set_hints(self, hints):
        self.footer_text.set_text(hints)

    def _restore_footer(self):
        self.frame.footer = self.footer

    def is_active(self, view):
        return not self.views or self.views[self._active] is view


def _set_proc_complete(monkeypatch, proc_module, complete):
    if complete:
        evidence = proc_module.AncestorProbe(frozenset({999}))
    else:
        issue = proc_module.ProcIssue(
            "process ancestors",
            "/proc",
            "unavailable",
        )
        evidence = proc_module.AncestorProbe(frozenset(), (issue,))
    monkeypatch.setattr(
        proc_module,
        "probe_current_ancestors",
        lambda: evidence,
    )


def _make_session(**overrides):
    view_defaults = dict(label="test session", mtime=1700000000.0, prompts=5)
    view_defaults.update(overrides)
    return make_session(**view_defaults)


def _make_project(**overrides):
    defaults = dict(
        name="myproj",
        directory="/tmp/myproj",
    )
    defaults.update(overrides)
    return Project(**defaults)


def _row_text(row):
    canvas = row.render((120,), focus=False)
    return b"\n".join(canvas.text).decode()


def _refresh_batch(
    snapshot: WorldSnapshot | None = None,
    *,
    plan: CleanupPlan | None = None,
    ordered_projects: tuple[Project, ...] | None = None,
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
            else tuple(snapshot.projects)
        ),
    )


def _apply_projects(
    view: ProjectsView,
    projects: list[Project],
    *,
    membership_issues=(),
) -> None:
    snapshot = WorldSnapshot(
        projects=projects,
        membership_issues=membership_issues,
    )
    view.apply_refresh(_refresh_batch(snapshot, ordered_projects=tuple(projects)))
