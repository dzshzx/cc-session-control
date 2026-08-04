"""Focused presentation tests for Sessions tmux-residency evidence."""

import urwid

from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.refresh import RefreshBatch
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import Session
from cc_session_control.views._keytable import help_lines
from cc_session_control.views.sessions import SessionRow, SessionsView


def _session(**overrides: object) -> Session:
    values: dict[str, object] = {
        "sid": "session-1",
        "cwd": "/tmp/project",
        "label": "session",
        "mtime": 1.0,
        "prompts": 1,
        "pid": 123,
        "alive": True,
        "current": False,
        "source": "cli",
        "status": "idle",
    }
    values.update(overrides)
    return Session(**values)  # type: ignore[arg-type]


def _row_text(session: Session) -> str:
    canvas = SessionRow(session).render((120,), focus=False)
    return b"\n".join(canvas.text).decode()


def _status_cell(session: Session) -> str:
    rendered = _row_text(session)
    _, end, _, _ = urwid.calc_trim_text(rendered, 0, len(rendered), 0, 8)
    return rendered[:end].rstrip()


def _status_text(sessions: list[Session]) -> str:
    view = SessionsView(object())  # type: ignore[arg-type]
    plan = CleanupPlan()
    view.apply_refresh(
        RefreshBatch(
            generation=1,
            snapshot=WorldSnapshot(sessions=tuple(sessions)),
            cleanup_plan=plan,
            cleanup_counts=plan.counts(),
            session_stats={"empty": 0, "short": 0, "orphans": 0},
            ordered_projects=(),
        )
    )
    return view.status.original_widget.get_text()[0]


def test_alive_rows_distinguish_resident_bare_and_unknown_at_stable_width() -> None:
    sessions = (
        _session(tmux_target="project:1"),
        _session(),
        _session(
            tmux_inventory_complete=False,
            tmux_inventory_detail="lost server connection",
        ),
    )
    resident, bare, unknown = map(_status_cell, sessions)

    assert resident == " ● 闲 ⧉"
    assert bare == " ● 闲"
    assert unknown == " ● 闲 ?"
    for rendered in map(_row_text, sessions):
        # Column anchors stay width-stable: the provider badge (cc) opens the
        # CLI column at 10; the 来源 badge ("CLI") starts after CLI col + gutter.
        provider_start = rendered.index("cc")
        assert urwid.calc_width(rendered, 0, provider_start) == 10
        source_start = rendered.index("CLI")
        assert urwid.calc_width(rendered, 0, source_start) == 15


def test_status_reports_deduplicated_tmux_inventory_degradation() -> None:
    detail = "lost server connection"
    degraded = [
        _session(
            sid="unknown-1", tmux_inventory_complete=False, tmux_inventory_detail=detail
        ),
        _session(
            sid="unknown-2", tmux_inventory_complete=False, tmux_inventory_detail=detail
        ),
    ]

    status = _status_text(degraded)

    assert "tmux 驻留未知 2" in status
    assert status.count(detail) == 1

    complete = _status_text([_session(tmux_inventory_detail="stale detail")])
    assert "tmux 驻留未知" not in complete
    assert "stale detail" not in complete


def test_help_explains_tmux_inventory_unknown_marker() -> None:
    help_text = "\n".join(help_lines(SessionsView.KEY_TABLE, SessionsView.HELP_LAYOUT))

    assert "? = tmux 驻留未知" in help_text
    assert "不能确认驻留或裸终端" in help_text
