"""Fourth-state hint for unbound live non-Claude processes (candidate B6).

Argv-exact liveness (ADR-0005) cannot bind bare-launched TUIs (`codex`,
`codex resume --last`, `kimi`, `kimi -c`, launcher-created sessions), so
their sessions read dead and a resume silently double-attaches. These tests
cover the honest middle ground: the `/proc` walk captures each CLI process's
cwd, providers flag the newest unbound row per directory
(`Session.unbound_live_hint`), the status column renders `? 未知` (and bound
non-Claude live rows `● 活`), resume verbs confirm the double-attach risk
first, and headless output tags the row `[live?]`. The hint NEVER upgrades
alive/kill semantics.
"""

from __future__ import annotations

import json

import pytest
from factories import make_session
from view_helpers import FakeApp

from cc_session_control.actions.resume_list import format_session
from cc_session_control.actions.session_ops import (
    ResumeIntent,
    TmuxResumeIntent,
    would_take_over,
)
from cc_session_control.config import cfg
from cc_session_control.data.proc import ProcCli, ProcCliInventory
from cc_session_control.data.providers import codex as codex_mod
from cc_session_control.data.providers import kimi as kimi_mod
from cc_session_control.data.providers.codex import CodexProvider
from cc_session_control.data.providers.kimi import KimiProvider
from cc_session_control.views._session_row import SessionRow, _status_parts
from cc_session_control.views.sessions import SessionsView

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"
UUID2 = "019fc790-3126-7601-9e9b-fd378524ada8"


def _proc(pid: int, *argv: str, starttime: str = "100", cwd: str = "") -> ProcCli:
    return ProcCli(pid=pid, argv=tuple(argv), starttime=starttime, cwd=cwd)


# --- /proc capture: cwd rides the one argv walk -----------------------------


class TestScanCapturesCwd:
    @staticmethod
    def _fake_proc(tmp_path, pid: int, cmdline: bytes, starttime: str = "777"):
        pid_dir = tmp_path / str(pid)
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "cmdline").write_bytes(cmdline)
        tail = ["S", "1"] + ["0"] * 17 + [starttime, "0", "0"]
        (pid_dir / "stat").write_text(f"{pid} (comm) " + " ".join(tail))
        return pid_dir

    def test_captures_cwd_of_matched_cli_process(self, tmp_path, monkeypatch):
        from cc_session_control.data import proc as proc_mod

        target = tmp_path / "workdir"
        target.mkdir()
        pid_dir = self._fake_proc(tmp_path, 42, b"codex\x00")
        (pid_dir / "cwd").symlink_to(target)
        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path))
        inventory = proc_mod.scan_cli_argv_inventory(frozenset({"codex"}))
        assert inventory.complete
        (record,) = inventory.records
        assert record.cwd == str(target)

    def test_missing_cwd_link_keeps_record_without_cwd(self, tmp_path, monkeypatch):
        # A vanished/never-created cwd link degrades ONLY the hint source —
        # the argv record itself must survive (bound liveness unaffected) and
        # the walk must stay complete (no issue: an incomplete inventory
        # would disable execution-time takeovers, a far worse trade).
        from cc_session_control.data import proc as proc_mod

        self._fake_proc(tmp_path, 42, b"codex\x00resume\x00abc\x00")
        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path))
        inventory = proc_mod.scan_cli_argv_inventory(frozenset({"codex"}))
        assert inventory.complete
        (record,) = inventory.records
        assert record.argv == ("codex", "resume", "abc")
        assert record.cwd == ""

    def test_unreadable_cwd_stays_silent_and_keeps_record(self, tmp_path, monkeypatch):
        # readlink failing on an existing entry (here EINVAL via a regular
        # file; EACCES on real systems) is silently ignored the same way.
        from cc_session_control.data import proc as proc_mod

        pid_dir = self._fake_proc(tmp_path, 42, b"kimi\x00")
        (pid_dir / "cwd").write_text("not a symlink")
        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path))
        inventory = proc_mod.scan_cli_argv_inventory(frozenset({"kimi"}))
        assert inventory.complete
        (record,) = inventory.records
        assert record.cwd == ""


# --- pure TUI-process predicates (daemons never enter the hint source) ------


class TestCodexTuiShape:
    def test_session_holding_shapes_match(self):
        assert codex_mod.is_tui_process(_proc(1, "codex"))
        assert codex_mod.is_tui_process(_proc(1, "codex", "resume"))
        assert codex_mod.is_tui_process(_proc(1, "codex", "resume", "--last"))
        assert codex_mod.is_tui_process(_proc(1, "codex", "fork", UUID1))
        assert codex_mod.is_tui_process(_proc(1, "codex", "写个脚本"))

    def test_daemon_and_utility_shapes_never_match(self):
        assert not codex_mod.is_tui_process(_proc(1, "codex", "app-server"))
        assert not codex_mod.is_tui_process(_proc(1, "codex", "exec", "do stuff"))
        assert not codex_mod.is_tui_process(_proc(1, "codex", "mcp-server"))
        assert not codex_mod.is_tui_process(_proc(1, "codex", "proxy"))
        assert not codex_mod.is_tui_process(_proc(1, "codex", "review"))

    def test_wrong_basename_never_matches(self):
        assert not codex_mod.is_tui_process(_proc(1, "kimi"))
        assert not codex_mod.is_tui_process(_proc(1))


class TestKimiTuiShape:
    def test_session_holding_shapes_match(self):
        assert kimi_mod.is_tui_process(_proc(1, "kimi"))
        assert kimi_mod.is_tui_process(_proc(1, "kimi", "--continue"))
        # bare interactive picker
        assert kimi_mod.is_tui_process(_proc(1, "kimi", "-S"))

    def test_title_rewritten_repl_matches_via_identity_set(self):
        # C1: the observed 0.31.1 rewrite — cmdline collapses to `kimi-code`,
        # comm follows, exe still points at the real binary.
        rewritten = ProcCli(
            pid=1,
            argv=("kimi-code",),
            starttime="1",
            comm="kimi-code",
            exe="/home/x/.kimi-code/bin/kimi",
        )
        assert kimi_mod.is_tui_process(rewritten)

    def test_server_and_headless_shapes_never_match(self):
        assert not kimi_mod.is_tui_process(_proc(1, "kimi", "web"))
        assert not kimi_mod.is_tui_process(_proc(1, "kimi", "acp"))
        assert not kimi_mod.is_tui_process(_proc(1, "kimi", "-p", "one prompt"))

    def test_wrong_identity_never_matches(self):
        assert not kimi_mod.is_tui_process(_proc(1, "codex"))
        assert not kimi_mod.is_tui_process(_proc(1))


# --- hint detection through provider discovery ------------------------------


def _write_rollout(root, day: str, name: str, payload: dict, mtime: float) -> None:
    import os

    directory = root / "sessions" / "2026" / "08" / day
    directory.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": "t", "type": "session_meta", "payload": payload}
    path = directory / name
    path.write_text(json.dumps(record) + "\n")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    home = tmp_path / "kimi"
    home.mkdir()
    monkeypatch.setattr(cfg, "kimi_home", home)
    return home


def _codex_payload(sid: str, cwd: str) -> dict:
    return {"id": sid, "session_id": sid, "cwd": cwd, "thread_source": "user"}


class TestCodexHintDetection:
    def test_newest_row_in_unbound_cwd_is_flagged(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"r-{UUID1}.jsonl",
            _codex_payload(UUID1, "/tmp/proj"),
            1000,
        )
        _write_rollout(
            codex_home,
            "02",
            f"r-{UUID2}.jsonl",
            _codex_payload(UUID2, "/tmp/proj"),
            2000,
        )
        inventory = ProcCliInventory(records=(_proc(9, "codex", cwd="/tmp/proj"),))
        scan = CodexProvider().discover(inventory, cur=frozenset())
        by_sid = {row.sid: row for row in scan.sessions}
        assert by_sid[UUID2].unbound_live_hint
        assert not by_sid[UUID1].unbound_live_hint  # older sibling stays clean
        assert not by_sid[UUID2].alive  # the hint never upgrades liveness
        assert would_take_over(by_sid[UUID2]) is False

    def test_bound_rows_never_flagged_hint_goes_to_newest_dead(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"r-{UUID1}.jsonl",
            _codex_payload(UUID1, "/tmp/proj"),
            1000,
        )
        _write_rollout(
            codex_home,
            "02",
            f"r-{UUID2}.jsonl",
            _codex_payload(UUID2, "/tmp/proj"),
            2000,
        )
        inventory = ProcCliInventory(
            records=(
                _proc(5, "codex", "resume", UUID2, cwd="/tmp/proj"),  # bound, alive
                _proc(9, "codex", cwd="/tmp/proj"),  # bare TUI, unbound
            ),
        )
        scan = CodexProvider().discover(inventory, cur=frozenset())
        by_sid = {row.sid: row for row in scan.sessions}
        assert by_sid[UUID2].alive and not by_sid[UUID2].unbound_live_hint
        assert by_sid[UUID1].unbound_live_hint  # newest NON-alive row

    def test_daemon_process_never_creates_hint(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"r-{UUID1}.jsonl",
            _codex_payload(UUID1, "/tmp/proj"),
            1000,
        )
        inventory = ProcCliInventory(
            records=(_proc(9, "codex", "app-server", cwd="/tmp/proj"),),
        )
        scan = CodexProvider().discover(inventory, cur=frozenset())
        (row,) = scan.sessions
        assert not row.unbound_live_hint

    def test_no_unbound_process_means_zero_flags(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"r-{UUID1}.jsonl",
            _codex_payload(UUID1, "/tmp/proj"),
            1000,
        )
        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert not row.unbound_live_hint

    def test_process_without_cwd_is_silently_ignored(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"r-{UUID1}.jsonl",
            _codex_payload(UUID1, "/tmp/proj"),
            1000,
        )
        inventory = ProcCliInventory(records=(_proc(9, "codex", cwd=""),))
        scan = CodexProvider().discover(inventory, cur=frozenset())
        (row,) = scan.sessions
        assert not row.unbound_live_hint

    def test_other_directory_never_flagged(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"r-{UUID1}.jsonl",
            _codex_payload(UUID1, "/tmp/proj"),
            1000,
        )
        inventory = ProcCliInventory(records=(_proc(9, "codex", cwd="/tmp/other"),))
        scan = CodexProvider().discover(inventory, cur=frozenset())
        (row,) = scan.sessions
        assert not row.unbound_live_hint


def _write_kimi_session(home, sid: str, work_dir: str, mtime: float) -> None:
    import os

    session_dir = home / "sessions" / "wd_x_123" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    state = session_dir / "state.json"
    state.write_text(json.dumps({"title": "t", "workDir": work_dir}))
    os.utime(state, (mtime, mtime))
    with open(home / "session_index.jsonl", "a") as fh:
        fh.write(json.dumps({"sessionId": sid, "sessionDir": str(session_dir)}) + "\n")


class TestKimiHintDetection:
    def test_bare_kimi_process_flags_newest_row(self, kimi_home):
        old = f"session_{UUID1}"
        new = f"session_{UUID2}"
        _write_kimi_session(kimi_home, old, "/tmp/proj", 1000)
        _write_kimi_session(kimi_home, new, "/tmp/proj", 2000)
        inventory = ProcCliInventory(records=(_proc(7, "kimi", cwd="/tmp/proj"),))
        scan = KimiProvider().discover(inventory, cur=frozenset())
        by_sid = {row.sid: row for row in scan.sessions}
        assert by_sid[new].unbound_live_hint
        assert not by_sid[old].unbound_live_hint
        assert not by_sid[new].alive

    def test_codex_process_never_flags_kimi_rows(self, kimi_home):
        sid = f"session_{UUID1}"
        _write_kimi_session(kimi_home, sid, "/tmp/proj", 1000)
        inventory = ProcCliInventory(records=(_proc(9, "codex", cwd="/tmp/proj"),))
        scan = KimiProvider().discover(inventory, cur=frozenset())
        (row,) = scan.sessions
        assert not row.unbound_live_hint


# --- status-column rendering ------------------------------------------------


class TestStatusRendering:
    def test_hint_row_renders_fourth_state(self):
        s = make_session(provider="codex", alive=False, unbound_live_hint=True)
        assert _status_parts(s) == (" ? 未知", "status_err")

    def test_bound_non_claude_live_renders_huo(self):
        codex = make_session(provider="codex", alive=True, pid=5)
        kimi = make_session(provider="kimi", alive=True, pid=6)
        assert _status_parts(codex) == (" ● 活", "alive")
        assert _status_parts(kimi) == (" ● 活", "alive")

    def test_non_claude_live_keeps_residency_badges(self):
        resident = make_session(
            provider="codex", alive=True, pid=5, tmux_target="proj:1"
        )
        unknown = make_session(
            provider="codex", alive=True, pid=5, tmux_inventory_complete=False
        )
        assert _status_parts(resident) == (" ● 活 ⧉", "alive")
        assert _status_parts(unknown) == (" ● 活 ?", "alive")

    def test_claude_rows_unchanged(self):
        busy = make_session(alive=True, pid=1, status="busy")
        idle = make_session(alive=True, pid=1, status="idle")
        dead = make_session(alive=False)
        assert _status_parts(busy) == (" ● 忙", "status_busy")
        assert _status_parts(idle) == (" ● 闲", "alive")
        assert _status_parts(dead) == (" ○ 停", "dead")

    def test_non_claude_dead_without_hint_unchanged(self):
        s = make_session(provider="kimi", alive=False)
        assert _status_parts(s) == (" ○ 停", "dead")

    def test_hint_row_widget_renders_with_and_without_focus(self):
        s = make_session(provider="codex", alive=False, unbound_live_hint=True)
        row = SessionRow(s)
        text = b"\n".join(row.render((120,), focus=False).text).decode()
        assert "? 未知" in text
        row.render((120,), focus=True)  # focus map must cover status_err


# --- honest confirmation on resume verbs ------------------------------------


def _hint_session(**overrides):
    values = dict(
        sid=UUID1,
        provider="codex",
        alive=False,
        current=False,
        pid=None,
        unbound_live_hint=True,
        label="调研任务",
    )
    values.update(overrides)
    return make_session(**values)


def _view_with(session):
    app = FakeApp()
    view = SessionsView(app)
    app.views = [view]
    view._sessions = [session]
    view._all_sessions = [session]
    view._rebuild()
    return app, view


class TestHintConfirm:
    def test_enter_confirms_double_attach_then_resumes_without_kill(self):
        s = _hint_session()
        app, view = _view_with(s)
        view.handle_key("enter")
        assert app.result is None  # blocked on the honest confirm
        assert "接回会话" in app._confirm_messages[-1]
        assert "codex" in app._confirm_messages[-1]
        assert "双开" in app._confirm_messages[-1]
        assert would_take_over(s) is False  # advisory only — nothing to kill
        app._last_confirm()
        assert app.result == TmuxResumeIntent(s)

    def test_t_confirms_double_attach_then_terminal_resume(self):
        s = _hint_session(provider="kimi", sid=f"session_{UUID1}")
        app, view = _view_with(s)
        view.handle_key("t")
        assert app.result is None
        assert "终端接回会话" in app._confirm_messages[-1]
        assert "kimi" in app._confirm_messages[-1]
        assert "双开" in app._confirm_messages[-1]
        app._last_confirm()
        assert app.result == ResumeIntent(s, fork=False)

    def test_R_confirms_double_attach_before_background(self, monkeypatch):
        import cc_session_control.views.sessions as sv_mod

        relaunched = {"n": 0}
        monkeypatch.setattr(
            sv_mod.tui_actions.session_ops,
            "do_tmux_resume_result",
            lambda s: (
                relaunched.__setitem__("n", relaunched["n"] + 1)
                or sv_mod.tui_actions.session_ops.TmuxResumeOutcome("proj:1")
            ),
        )
        s = _hint_session()
        app, view = _view_with(s)
        view.handle_key("R")
        assert relaunched["n"] == 0
        assert "转入后台" in app._confirm_messages[-1]
        assert "双开" in app._confirm_messages[-1]
        app._last_confirm()
        assert relaunched["n"] == 1

    def test_fork_never_confirms_even_on_hint_rows(self):
        s = _hint_session()
        app, view = _view_with(s)
        view._key_fork(s)
        assert app._confirm_messages == []
        assert app.result == TmuxResumeIntent(s, fork=True)

    def test_dead_row_without_hint_still_resumes_without_confirm(self):
        s = make_session(provider="codex", sid=UUID1, alive=False)
        app, view = _view_with(s)
        view.handle_key("enter")
        assert app._confirm_messages == []
        assert app.result == TmuxResumeIntent(s)
        app2, view2 = _view_with(s)
        view2.handle_key("t")
        assert app2._confirm_messages == []
        assert app2.result == ResumeIntent(s, fork=False)

    def test_stop_still_refuses_hint_rows_as_not_running(self):
        s = _hint_session()
        app, view = _view_with(s)
        view._key_stop(s)
        assert app._confirm_messages == []
        assert any("未在运行" in n for n in app._notifications)


# --- headless `csctl resume` face -------------------------------------------


class TestHeadlessLabel:
    def test_hint_row_tagged_live_question_with_note(self):
        s = _hint_session(cwd="/tmp/proj")
        lines = format_session(s)
        assert lines[0].startswith("[live?]")
        assert any(
            "unbound codex process" in line and "double-attach" in line
            for line in lines
        )

    def test_dead_and_live_rows_unchanged(self):
        dead = make_session(provider="codex", sid=UUID1, alive=False)
        dead_lines = format_session(dead)
        assert dead_lines[0].startswith("[dead]")
        assert not any("double-attach" in line for line in dead_lines)
        live = make_session(provider="codex", sid=UUID1, alive=True, pid=3)
        assert format_session(live)[0].startswith("[live]")
