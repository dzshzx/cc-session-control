"""Codex `archived_sessions/` discovery + honest verb refusals (B7).

`~/.codex/archived_sessions/` is a FLAT directory of rollout files (same
first-line `session_meta` NDJSON contract as the active date tree, no date
subtree). Discovery lists archived rows (`Session.archived`) so operators can
find and search them (hidden by default in the Sessions view, `a` reveals);
the resume family refuses them honestly and hands back
the official `codex unarchive <sid>` recovery command — `codex resume`
straight against an archived rollout is unverified upstream semantics.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from factories import make_session

from cc_session_control.actions import session_ops, tui_actions
from cc_session_control.actions.resume_list import format_session
from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.proc import ProcCliInventory
from cc_session_control.data.providers.codex import CodexProvider

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"
UUID2 = "019fc790-3126-7601-9e9b-fd378524ada8"


def _archived_session(**overrides):
    base = dict(provider="codex", sid=UUID1, archived=True, label="旧任务")
    base.update(overrides)
    return make_session(**base)


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


def _payload(sid: str, **overrides) -> dict:
    payload = {
        "id": sid,
        "session_id": sid,
        "cwd": "/tmp/proj",
        "thread_source": "user",
    }
    payload.update(overrides)
    return payload


def _meta_line(payload: dict) -> str:
    record = {"timestamp": "t", "type": "session_meta", "payload": payload}
    return json.dumps(record) + "\n"


def _write_active(home, sid: str, payload: dict | None = None) -> Path:
    directory = home / "sessions" / "2026" / "08" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-a-{sid}.jsonl"
    path.write_text(_meta_line(payload if payload is not None else _payload(sid)))
    return path


def _write_archived(
    home,
    sid: str,
    payload: dict | None = None,
    body: str = "",
) -> Path:
    # Real upstream layout: archived rollouts sit FLAT in archived_sessions/.
    directory = home / "archived_sessions"
    directory.mkdir(exist_ok=True)
    path = directory / f"rollout-2026-06-15T14-29-51-{sid}.jsonl"
    path.write_text(
        _meta_line(payload if payload is not None else _payload(sid)) + body
    )
    return path


def _scan(cur: frozenset[int] = frozenset()):
    return CodexProvider().discover(ProcCliInventory(), cur=cur)


class TestArchivedDiscovery:
    def test_archived_rows_list_flagged_alongside_active(self, codex_home):
        _write_active(codex_home, UUID1)
        _write_archived(codex_home, UUID2, _payload(UUID2, cwd="/tmp/old-proj"))
        scan = _scan()
        assert scan.complete
        rows = {row.sid: row for row in scan.sessions}
        assert rows[UUID2].archived
        assert rows[UUID2].cwd == "/tmp/old-proj"
        assert not rows[UUID1].archived

    def test_archived_subagent_rollouts_are_skipped(self, codex_home):
        _write_archived(
            codex_home,
            UUID2,
            _payload(UUID2, thread_source="subagent"),
        )
        assert _scan().sessions == ()

    def test_archived_label_falls_back_to_first_user_message(self, codex_home):
        body = (
            json.dumps(
                {
                    "timestamp": "t",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "修归档发现"},
                }
            )
            + "\n"
        )
        _write_archived(codex_home, UUID1, body=body)
        (row,) = _scan().sessions
        assert row.label == "修归档发现"

    def test_archived_source_classification_reuses_the_one_parse_path(
        self,
        codex_home,
    ):
        _write_archived(codex_home, UUID1, _payload(UUID1, originator="codex_exec"))
        (row,) = _scan().sessions
        assert row.source == "sdk" and row.archived

    def test_same_sid_in_both_trees_active_wins(self, codex_home):
        active = _write_active(codex_home, UUID1, _payload(UUID1, cwd="/tmp/live"))
        archived = _write_archived(
            codex_home,
            UUID1,
            _payload(UUID1, cwd="/tmp/stale"),
        )
        # Even a NEWER archived copy loses: archive is a move upstream, so a
        # double listing is stale evidence and the active row is the truth.
        os.utime(active, (1000, 1000))
        os.utime(archived, (2000, 2000))
        (row,) = _scan().sessions
        assert not row.archived
        assert row.cwd == "/tmp/live" and row.mtime == 1000

    def test_missing_archived_dir_is_normal_zero_issues(self, codex_home):
        _write_active(codex_home, UUID1)
        scan = _scan()
        assert scan.complete and scan.issues == ()
        (row,) = scan.sessions
        assert not row.archived

    def test_unreadable_archived_dir_degrades_without_hurting_active(
        self,
        codex_home,
    ):
        _write_active(codex_home, UUID1)
        _write_archived(codex_home, UUID2)
        archived_root = codex_home / "archived_sessions"
        os.chmod(archived_root, 0)
        try:
            scan = _scan()
        finally:
            os.chmod(archived_root, 0o755)
        assert not scan.complete
        assert any("archived_sessions" in (i.path or "") for i in scan.issues)
        (row,) = scan.sessions  # the active tree still lists
        assert row.sid == UUID1 and not row.archived

    def test_archived_only_home_still_lists(self, tmp_path, monkeypatch):
        home = tmp_path / "codex-archived-only"
        home.mkdir()  # no sessions/ tree at all — e.g. wiped active state
        monkeypatch.setattr(cfg, "codex_home", home)
        _write_archived(home, UUID1)
        (row,) = _scan().sessions
        assert row.archived


class TestArchivedCommands:
    def test_provider_unarchive_argv_is_the_official_verb(self):
        assert CodexProvider().unarchive_argv(UUID1) == ["codex", "unarchive", UUID1]

    def test_registry_dispatch_is_loud_without_archive_verbs(self):
        with pytest.raises(TypeError, match="archive verbs"):
            providers.unarchive_argv("claude", UUID1)

    def test_resume_cmd_archived_is_the_unarchive_command(self):
        # No `cd` prefix either: unarchive acts on codex's own store, not a cwd.
        cmd = session_ops.resume_cmd(_archived_session())
        assert cmd == f"codex unarchive {UUID1}"

    def test_copy_notify_says_unarchive(self, monkeypatch):
        copied: list[str] = []
        monkeypatch.setattr(
            session_ops,
            "to_clipboard",
            lambda text: copied.append(text) or True,
        )
        result = tui_actions.copy_resume_command(_archived_session())
        assert copied == [f"codex unarchive {UUID1}"]
        assert result.message == "已复制 unarchive 命令（会话已归档）"


class TestArchivedHeadless:
    def test_format_marks_archived_and_hands_back_unarchive(self):
        lines = format_session(_archived_session())
        assert "[codex] (archived)" in lines[0]
        assert lines[2] == f"    codex unarchive {UUID1}"
        assert lines[3] == "    ^ archived — unarchive first, then resume"

    def test_non_archived_codex_line_has_no_archived_tag(self):
        lines = format_session(make_session(provider="codex", sid=UUID1))
        assert "(archived)" not in lines[0]


class TestArchivedViewSurface:
    def test_row_shows_archived_marker(self):
        from view_helpers import _row_text

        from cc_session_control.views._session_row import SessionRow

        assert "[归档]" in _row_text(SessionRow(_archived_session()))
        plain = make_session(provider="codex", sid=UUID1, label="旧任务")
        assert "归档" not in _row_text(SessionRow(plain))

    def test_archived_rows_hidden_by_default_and_revealed_by_a_key(self):
        from view_helpers import FakeApp

        from cc_session_control.views.sessions import SessionsView

        view = SessionsView(FakeApp())
        archived = _archived_session()
        plain = make_session(provider="codex", sid=UUID2)
        view._all_sessions = [archived, plain]
        view._apply_filter()
        view._rebuild()
        assert view._sessions == [plain]
        assert "归档已隐藏 1" in view.status.original_widget.get_text()[0]

        view.handle_key("a")

        assert view._sessions == [archived, plain]
        assert "归档 1" in view.status.original_widget.get_text()[0]

    def test_filter_matches_the_archived_marker(self):
        from view_helpers import FakeApp

        from cc_session_control.views.sessions import SessionsView

        view = SessionsView(FakeApp())
        archived = _archived_session()
        view._all_sessions = [archived, make_session(provider="codex", sid=UUID2)]
        view._show_archived = True
        view._filter_text = "归档"
        view._apply_filter()
        assert view._sessions == [archived]

    @pytest.mark.parametrize(
        "handler",
        ["_key_resume", "_key_terminal", "_key_relaunch", "_key_fork"],
    )
    def test_resume_family_refuses_dead_archived_rows(self, handler):
        from view_helpers import FakeApp

        from cc_session_control.views.sessions import SessionsView

        app = FakeApp()
        view = SessionsView(app)
        getattr(view, handler)(_archived_session())
        # Refusal, not a modal and not an exit intent: nothing to confirm,
        # nothing gets resumed/forked/killed.
        assert app.result is None
        assert app._confirm_messages == []
        (notice,) = app._notifications
        assert notice == (
            f"该会话已归档：先 codex unarchive {UUID1[:8]}… 恢复后再接回。"
        )
