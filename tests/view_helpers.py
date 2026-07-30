"""Shared plain helpers (FakeApp stub + builders) for the split `test_views*` modules."""

from pathlib import Path

import urwid
from factories import make_session

from cc_session_control.actions.runner import Accepted
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
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
    TrustDecision,
)
from cc_session_control.views.rc import RCView


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
        trust_decision=TrustDecision.TRUSTED,
        status="stopped",
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
