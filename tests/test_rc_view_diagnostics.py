"""Status diagnostics for the RC operator view."""

from cc_session_control.data import environment_ledger as ledger
from cc_session_control.data import environments
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.rc_enabled import (
    EnabledListOperation,
    EnabledListResult,
    EnabledListStage,
    EnabledListState,
)
from cc_session_control.data.refresh import RefreshBatch
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import RCProject
from cc_session_control.views.rc import RCView


class _ViewApp:
    def __init__(self) -> None:
        self.views: list[RCView] = []


def _make_project(name: str) -> RCProject:
    return RCProject(
        name=name,
        directory="/tmp/myproj",
        trusted=True,
        in_list=True,
        status="stopped",
        auto_start=True,
    )


def _refresh_batch(snapshot: WorldSnapshot) -> RefreshBatch:
    plan = CleanupPlan()
    return RefreshBatch(
        generation=1,
        snapshot=snapshot,
        cleanup_plan=plan,
        cleanup_counts=plan.counts(),
        session_stats={},
        ordered_projects=tuple(snapshot.rc_projects),
    )


def _row_text(row) -> str:
    canvas = row.render((120,), focus=False)
    return b"\n".join(canvas.text).decode()


def test_rc_view_status_counts_typed_ledger_warning_and_failure():
    app = _ViewApp()
    view = RCView(app)
    app.views = [view]
    snap = WorldSnapshot(
        rc_projects=[_make_project(name="p1")],
        environment_reconciliation=environments.Reconciliation(
            ledger_history_complete=False,
            ledger=ledger.LedgerUpdate(
                ledger.LedgerUpdateState.FAILED,
                read=ledger.LedgerRead(
                    ledger.LedgerReadState.PARTIAL,
                    warnings=(ledger.LedgerWarning(3, "bad row"),),
                ),
                failure=ledger.LedgerFailure.READ,
                detail="permission denied",
            ),
        ),
    )

    view.apply_refresh(_refresh_batch(snap))

    assert "⚠ 环境台账异常 2" in view.status.original_widget.get_text()[0]


def test_rc_view_status_exposes_exact_enabled_list_failure() -> None:
    failure = EnabledListResult(
        EnabledListOperation.LIST,
        EnabledListState.FAILED,
        None,
        changed=False,
        committed=False,
        stage=EnabledListStage.READ,
        detail="permission denied",
    )
    snap = WorldSnapshot(
        rc_projects=[_make_project(name="trusted")],
        rc_enabled_list=failure,
    )
    app = _ViewApp()
    view = RCView(app)
    app.views = [view]

    view.apply_refresh(_refresh_batch(snap))

    status = view.status.original_widget.get_text()[0]
    assert "⚠ RC 清单不完整 1" in status
    assert "自启列表 read：permission denied" in status
    assert "trusted" in _row_text(view.walker[0])


def test_rc_view_status_does_not_count_missing_unchanged_ledger():
    app = _ViewApp()
    view = RCView(app)
    app.views = [view]

    view.apply_refresh(
        _refresh_batch(
            WorldSnapshot(
                environment_reconciliation=environments.Reconciliation(),
            ),
        ),
    )

    assert "环境台账异常" not in view.status.original_widget.get_text()[0]
