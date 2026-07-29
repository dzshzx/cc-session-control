"""View unit tests: Sessions cleanup submenu — preview/confirm for empty/short/orphan/zombie/aged sweeps, delete feedback, and refresh-vs-preview anchoring."""

import os

import pytest
from view_helpers import (
    FakeApp,
    _make_session,
    _refresh_batch,
    _row_text,
    _set_proc_complete,
)

from cc_session_control.actions.runner import Busy, Closed
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.refresh import RefreshFailure
from cc_session_control.models import (
    Session,
)
from cc_session_control.views._confirm import DEGRADED
from cc_session_control.views.sessions import SessionsView


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

    monkeypatch.setattr(
        cl_mod.proc,
        "probe_current_ancestors",
        lambda: cl_mod.proc.AncestorProbe(frozenset({999})),
    )
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

    assert view._preview is not None
    assert {s.sid for s in view._preview.targets if isinstance(s, Session)} == {
        "short1",
        "short2",
    }


def test_cleanup_preview_label_limit_uses_terminal_cells(monkeypatch):
    import cc_session_control.views._sessions_cleanup as cleanup_view
    from cc_session_control.data.cleanup import CleanupPlan

    monkeypatch.setattr(
        cleanup_view.proc,
        "probe_current_ancestors",
        lambda: cleanup_view.proc.AncestorProbe(frozenset({999})),
    )
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


def _focus_dead_session(view, **overrides):
    overrides.setdefault("alive", False)
    view._all_sessions = [_make_session(**overrides)]
    view._apply_filter()
    view._rebuild()
    view.walker.set_focus(0)


def test_delete_honest_feedback_true_then_false(monkeypatch):
    # Fix 3 / L4: only claim 已删除 when remove_session truly removed something.
    import cc_session_control.views.sessions as sv_mod

    monkeypatch.setattr(
        sv_mod.proc,
        "probe_current_ancestors",
        lambda: sv_mod.proc.AncestorProbe(frozenset({999})),
    )
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

    monkeypatch.setattr(
        sv_mod.proc,
        "probe_current_ancestors",
        lambda: sv_mod.proc.AncestorProbe(frozenset({999})),
    )
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

    monkeypatch.setattr(
        sv_mod.proc,
        "probe_current_ancestors",
        lambda: sv_mod.proc.AncestorProbe(frozenset({999})),
    )
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

    _set_proc_complete(monkeypatch, sv_mod.proc, False)
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
    assert app._notifications[-1] == DEGRADED


def test_cleanup_preview_refuses_when_undeterminable_not_nothing(monkeypatch):
    # Fix 2a: a degraded refusal must NOT read as "无…需要清理".
    import cc_session_control.views.sessions as sv_mod

    _set_proc_complete(monkeypatch, sv_mod.proc, False)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._enter_preview("empty")
    assert view._mode == "list"  # never opened a preview
    assert app._notifications[-1] == DEGRADED
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

    monkeypatch.setattr(
        cl_mod.proc,
        "probe_current_ancestors",
        lambda: cl_mod.proc.AncestorProbe(frozenset({999})),
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(zombie_pids=[111])

    view._enter_preview("zombies")
    assert view._mode == "preview"
    assert view._preview is not None
    assert view._preview.action.key == "zombies"

    import dataclasses

    swept = {}
    from cc_session_control.data.removal import CleanupExecution

    view._preview = dataclasses.replace(
        view._preview,
        action=dataclasses.replace(
            view._preview.action,
            execute=lambda plan, pids, **k: (
                swept.update(pids=pids) or CleanupExecution(completed=["111"])
            ),
        ),
    )
    view._confirm_cleanup()
    assert swept["pids"] == [111]  # exactly the previewed targets
    assert app._submitted_actions == ["session.cleanup.zombies"]
    assert view._mode == "cleanup"
    assert view._preview is None
    assert any("僵尸会话文件" in m for m in app._notifications)


def test_zombie_sweep_gated_when_undeterminable(monkeypatch):
    import cc_session_control.views.sessions as sv_mod
    from cc_session_control.data.cleanup import CleanupPlan

    _set_proc_complete(monkeypatch, sv_mod.proc, False)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(zombie_pids=[111])
    view._enter_preview("zombies")
    assert view._mode == "list"
    assert app._notifications[-1] == DEGRADED


def test_aged_sweep_preview_and_confirm_not_gated(monkeypatch):
    # Fix 4: the age sweep is mtime-only -> NOT R10-gated; works even with no /proc.
    import cc_session_control.views._sessions_cleanup as cl_mod
    from cc_session_control.data.cleanup import CleanupPlan

    monkeypatch.setattr(
        cl_mod.proc,
        "probe_current_ancestors",
        lambda: cl_mod.proc.AncestorProbe(
            frozenset(),
            (cl_mod.proc.ProcIssue("process ancestors", "/proc", "unavailable"),),
        ),
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(aged_entries=["shell-snapshots/old.sh"])

    view._enter_preview("aged")
    assert view._mode == "preview"
    assert view._preview is not None
    assert view._preview.action.key == "aged"

    import dataclasses

    swept = {}
    from cc_session_control.data.removal import CleanupExecution

    view._preview = dataclasses.replace(
        view._preview,
        action=dataclasses.replace(
            view._preview.action,
            execute=lambda plan, entries, **k: (
                swept.update(entries=entries)
                or CleanupExecution(completed=["shell-snapshots/old.sh"])
            ),
        ),
    )
    view._confirm_cleanup()
    assert swept["entries"] == ["shell-snapshots/old.sh"]
    assert any("过期项" in m for m in app._notifications)


def test_successful_refresh_keeps_cleanup_preview_anchored_to_displayed_generation(
    monkeypatch,
    tmp_path,
):
    from cc_session_control.config import cfg
    from cc_session_control.data.removal import anchor_path

    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    target = cfg.shell_snapshots_dir / "old"
    target.parent.mkdir(parents=True)
    target.write_text("previewed")
    os.utime(target, (1, 1))
    entry = "shell-snapshots/old"
    plan_a = CleanupPlan(
        aged_entries=[entry],
        aged_anchors={entry: anchor_path(cfg.shell_snapshots_dir, target)},
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.apply_refresh(_refresh_batch(plan=plan_a))
    view._enter_preview("aged")
    overlay = view._body.original_widget

    target.rename(target.with_name("previewed-away"))
    target.write_text("replacement")
    os.utime(target, (1, 1))
    plan_b = CleanupPlan(
        aged_entries=[entry],
        aged_anchors={entry: anchor_path(cfg.shell_snapshots_dir, target)},
    )

    view.apply_refresh(_refresh_batch(plan=plan_b))

    assert view._body.original_widget is overlay
    assert view._plan is plan_b
    assert view._preview is not None
    assert view._preview.plan is plan_a

    view._confirm_cleanup()

    assert target.read_text() == "replacement"
    assert "拒绝" in app._notifications[-1]


def test_cleanup_preview_escape_clears_pinned_generation():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    plan = CleanupPlan(aged_entries=["plans/old"])
    view.apply_refresh(_refresh_batch(plan=plan))
    view._enter_preview("aged")

    view.handle_key("esc")

    assert view._mode == "cleanup"
    assert view._preview is None


@pytest.mark.parametrize("outcome", [Busy("other"), Closed()])
def test_cleanup_preview_rejected_submission_keeps_pinned_generation(outcome):
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    plan = CleanupPlan(aged_entries=["plans/old"])
    view.apply_refresh(_refresh_batch(plan=plan))
    view._enter_preview("aged")
    preview = view._preview
    assert preview is not None
    app.submit_action = lambda *_args: outcome

    view._confirm_cleanup()

    assert view._mode == "preview"
    assert view._preview is preview


def test_new_cleanup_preview_pins_current_generation():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    plan_a = CleanupPlan(aged_entries=["plans/old"])
    plan_b = CleanupPlan(aged_entries=["plans/new"])
    view.apply_refresh(_refresh_batch(plan=plan_a))
    view._enter_preview("aged")
    view.handle_key("esc")
    view.apply_refresh(_refresh_batch(plan=plan_b))

    view._enter_preview("aged")

    assert view._preview is not None
    assert view._preview.targets == ("plans/new",)
    assert view._preview.plan is plan_b


def test_cleanup_confirmation_without_preview_is_noop():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]

    view._confirm_cleanup()

    assert app._submitted_actions == []
    assert view._mode == "list"
    assert view._preview is None


def test_refresh_failure_updates_open_cleanup_and_aged_preview_in_memory():
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._classified = {"aged_entries": 1}
    view._enter_cleanup()
    failure = RefreshFailure(
        2,
        "process stat",
        "unavailable",
        CleanupPlan(
            aged_entries=["plans/new"],
        ),
    )

    view.apply_refresh_failure(failure)

    aged_row = next(row for row in view._cleanup_walker if row.action_key == "aged")
    assert "1" in _row_text(aged_row)
    view._enter_preview("aged")
    assert view._preview is not None
    assert view._preview.targets == ("plans/new",)

    newer = RefreshFailure(
        3,
        "process stat",
        "still unavailable",
        CleanupPlan(aged_entries=["plans/newer"]),
    )
    view.apply_refresh_failure(newer)
    assert view._mode == "preview"
    assert view._preview is not None
    assert view._preview.targets == ("plans/newer",)
    assert view._preview.plan is newer.cleanup_plan


def test_refresh_failure_closes_session_keyed_preview_fail_closed(monkeypatch):
    import cc_session_control.views._sessions_cleanup as cl_mod

    _set_proc_complete(monkeypatch, cl_mod.proc, True)
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(short=[_make_session(sid="short", prompts=1)])
    view._enter_preview("short")
    assert view._mode == "preview"

    view.apply_refresh_failure(
        RefreshFailure(
            2,
            "process stat",
            "unavailable",
            CleanupPlan(aged_entries=["plans/old"]),
        )
    )

    assert view._mode == "cleanup"
    assert view._preview is None
    assert view._plan.short == ()
    assert view._plan.aged_entries == ("plans/old",)


def test_refresh_failure_refuses_session_preview_before_proc_and_keeps_aged(
    monkeypatch,
):
    import cc_session_control.views._sessions_cleanup as cl_mod

    proc_probes = []

    def probe_current_ancestors():
        proc_probes.append("called")
        return cl_mod.proc.AncestorProbe(frozenset({999}))

    monkeypatch.setattr(
        cl_mod.proc,
        "probe_current_ancestors",
        probe_current_ancestors,
    )
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view.apply_refresh_failure(
        RefreshFailure(
            2,
            "process stat (/proc/7/stat)",
            "process stat (/proc/7/stat): permission denied",
            CleanupPlan(aged_entries=["plans/old"]),
        )
    )

    view._enter_cleanup()

    menu_warning = app._notifications[-1]
    assert "清理预览不完整" in menu_warning
    assert "process stat (/proc/7/stat)" in menu_warning
    assert "permission denied" in menu_warning

    view._enter_preview("short")

    assert proc_probes == []
    refusal = app._notifications[-1]
    assert "预览不可用" in refusal
    assert "process stat (/proc/7/stat)" in refusal
    assert "permission denied" in refusal
    assert "无短会话(≤2提问)需要清理" not in refusal

    view._enter_preview("aged")

    assert view._mode == "preview"
    assert view._preview is not None
    assert view._preview.targets == ("plans/old",)


def test_cleanup_confirmation_reports_partial_failure(monkeypatch, tmp_path):
    import dataclasses

    import cc_session_control.views._sessions_cleanup as cl_mod
    from cc_session_control.data.cleanup import CleanupPlan
    from cc_session_control.data.removal import (
        CleanupExecution,
        PathRemoval,
        RemovalStatus,
    )

    monkeypatch.setattr(
        cl_mod.proc,
        "probe_current_ancestors",
        lambda: cl_mod.proc.AncestorProbe(frozenset({999})),
    )
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
    assert view._preview is not None
    view._preview = dataclasses.replace(
        view._preview,
        action=dataclasses.replace(
            view._preview.action,
            execute=lambda plan, targets: partial,
        ),
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


def test_aged_preview_does_not_claim_empty_when_age_source_is_unavailable():
    from cc_session_control.data.cleanup import CleanupIssue, CleanupPlan

    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._plan = CleanupPlan(issues=[CleanupIssue("aged_entries", "permission denied")])

    view._enter_preview("aged")

    assert "预览不可用" in app._notifications[-1]
    assert "permission denied" in app._notifications[-1]
    assert "无过期文件需要清理" not in app._notifications[-1]
