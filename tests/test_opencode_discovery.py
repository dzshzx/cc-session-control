"""opencode disk discovery + argv-exact liveness (ADR-0005)."""

from __future__ import annotations

import sqlite3

import pytest

from cc_session_control.config import cfg
from cc_session_control.data.proc import ProcCli, ProcCliInventory
from cc_session_control.data.providers import opencode as opencode_mod
from cc_session_control.data.providers.opencode import OpencodeProvider

SES1 = "ses_1aaaaaaaaaaaaaaaaaaa"
SES2 = "ses_2bbbbbbbbbbbbbbbbbbb"


def _proc(pid: int, *argv: str, starttime: str = "100", cwd: str = "") -> ProcCli:
    return ProcCli(pid=pid, argv=tuple(argv), starttime=starttime, cwd=cwd)


@pytest.fixture
def opencode_home(tmp_path, monkeypatch):
    home = tmp_path / "opencode"
    home.mkdir()
    monkeypatch.setattr(cfg, "opencode_home", home)
    return home


def _row(
    sid: str,
    directory: str = "/tmp/proj",
    title: str = "x",
    updated: int = 1_787_036_782_980,
    parent: str | None = None,
    archived: int | None = None,
) -> tuple:
    return (sid, parent, directory, title, updated, archived)


def _write_db(home, rows) -> None:
    """A minimal `session` table — only the columns discovery reads."""
    conn = sqlite3.connect(home / "opencode.db")
    conn.execute(
        "CREATE TABLE session ("
        "id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL,"
        " title TEXT NOT NULL, time_updated INTEGER NOT NULL, time_archived INTEGER)"
    )
    conn.executemany("INSERT INTO session VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class TestOpencodeExtract:
    def test_session_flag_matches(self):
        assert opencode_mod.extract_sid(("opencode", "--session", SES1)) == SES1
        assert opencode_mod.extract_sid(("opencode", "-s", SES1)) == SES1
        assert opencode_mod.extract_sid(("opencode", f"--session={SES1}")) == SES1

    def test_bare_and_flagless_never_match(self):
        assert opencode_mod.extract_sid(("opencode",)) is None
        assert opencode_mod.extract_sid(("opencode", "--continue")) is None
        assert opencode_mod.extract_sid(("opencode", "--session", "--fork")) is None
        assert opencode_mod.extract_sid(("kimi", "--session", SES1)) is None

    def test_headless_run_with_session_flag_still_binds(self):
        # `opencode run --session <sid>` is a headless one-shot, but while it
        # runs it IS the process holding that session — same binding rule as
        # kimi's `--session -p` print mode.
        assert opencode_mod.extract_sid(("opencode", "run", "--session", SES1)) == SES1


class TestOpencodeTuiPredicate:
    @pytest.mark.parametrize(
        "argv",
        [
            ("opencode",),
            ("opencode", "/tmp/proj"),
            ("opencode", "--continue"),
            ("opencode", "--session", SES1),
        ],
    )
    def test_session_holding_shapes(self, argv):
        assert opencode_mod.is_tui_process(_proc(1, *argv))

    @pytest.mark.parametrize(
        "argv",
        [
            ("opencode", "run", "hi"),
            ("opencode", "serve"),
            ("opencode", "web"),
            ("opencode", "attach", "http://localhost:4096"),
            ("opencode", "session", "list"),
            ("opencode", "session", "delete", SES1),
            ("opencode", "--help"),
            ("bash",),
        ],
    )
    def test_non_session_shapes(self, argv):
        assert not opencode_mod.is_tui_process(_proc(1, *argv))


class TestOpencodeDiscover:
    def test_projects_rows_with_liveness(self, opencode_home):
        _write_db(opencode_home, [_row(SES1, title="修缓存", updated=2_000_000)])
        inventory = ProcCliInventory(
            records=(_proc(7, "opencode", "--session", SES1, starttime="55"),),
        )
        scan = OpencodeProvider().discover(inventory, cur=frozenset())
        assert scan.complete
        (row,) = scan.sessions
        assert row.provider == "opencode"
        assert row.sid == SES1
        assert row.label == "修缓存"
        assert row.cwd == "/tmp/proj"
        assert row.mtime == 2_000.0  # epoch ms projected to seconds
        assert row.alive and row.pid == 7 and row.proc_start == "55"
        assert not row.current
        assert row.source == "cli"

    def test_current_pid_marks_current(self, opencode_home):
        _write_db(opencode_home, [_row(SES1)])
        inventory = ProcCliInventory(
            records=(_proc(42, "opencode", "-s", SES1),),
        )
        scan = OpencodeProvider().discover(inventory, cur=frozenset({42}))
        (row,) = scan.sessions
        assert row.current

    def test_subagent_and_archived_rows_are_skipped(self, opencode_home):
        _write_db(
            opencode_home,
            [
                _row(SES1, title="root"),
                _row(SES2, parent=SES1),  # subagent session
                _row("ses_3archived", archived=1_700_000_000_000),
            ],
        )
        scan = OpencodeProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert row.sid == SES1

    def test_empty_title_degrades_to_untitled(self, opencode_home):
        _write_db(opencode_home, [_row(SES1, title="")])
        scan = OpencodeProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert row.label == "(untitled)"

    def test_missing_database_means_no_rows(self, opencode_home):
        scan = OpencodeProvider().discover(ProcCliInventory(), cur=frozenset())
        assert scan.sessions == () and scan.complete

    def test_malformed_database_is_an_issue(self, opencode_home):
        (opencode_home / "opencode.db").write_text("not a sqlite database")
        scan = OpencodeProvider().discover(ProcCliInventory(), cur=frozenset())
        assert not scan.complete
        assert scan.sessions == ()
        assert any("opencode.db" in (i.path or "") for i in scan.issues)

    def test_unbound_bare_tui_hints_newest_row_in_its_cwd(self, opencode_home):
        _write_db(
            opencode_home,
            [
                _row(SES1, updated=1_000_000),
                _row(SES2, updated=2_000_000),
            ],
        )
        inventory = ProcCliInventory(
            records=(_proc(9, "opencode", cwd="/tmp/proj"),),
        )
        scan = OpencodeProvider().discover(inventory, cur=frozenset())
        by_sid = {row.sid: row for row in scan.sessions}
        assert not by_sid[SES1].unbound_live_hint
        assert by_sid[SES2].unbound_live_hint
        assert not by_sid[SES2].alive  # advisory only, never liveness

    def test_fresh_install_scans_clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "opencode_home", tmp_path / "opencode-fresh")
        (tmp_path / "opencode-fresh").mkdir()
        provider = OpencodeProvider()
        assert provider.available()
        scan = provider.discover(ProcCliInventory(), frozenset())
        assert scan.sessions == () and scan.complete
