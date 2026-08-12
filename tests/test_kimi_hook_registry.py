"""Kimi runtime-registry binding (hook-maintained `run/<pid>.json`).

kimi's official SessionStart/SessionEnd hooks (`csctl _kimi-hook`,
actions/kimi_hook) maintain one `<pid>.json` per live session under the kimi
home — the CLI's own self-report and the ONLY evidence covering bare TUIs
(argv dies to the title rewrite; dispatch metadata covers csctl windows
only). These tests pin both ends: the hook command's registry writes, and
the provider's verified join (stale/forged/ambiguous entries never bind).
"""

from __future__ import annotations

import io
import json

import pytest

from cc_session_control.actions import kimi_hook
from cc_session_control.cli import build_parser, main
from cc_session_control.cli_commands import cmd_kimi_hook
from cc_session_control.config import cfg
from cc_session_control.data.proc import ProcCli, ProcCliInventory
from cc_session_control.data.providers.kimi import KimiProvider

SID = "session_00000000-0000-4000-8000-000000000001"
SID2 = "session_00000000-0000-4000-8000-000000000002"


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    home = tmp_path / "kimi"
    home.mkdir()
    monkeypatch.setattr(cfg, "kimi_home", home)
    return home


def _registry_file(home, pid: int, sid: str = SID, proc_start: str = "555"):
    run = home / "run"
    run.mkdir(exist_ok=True)
    path = run / f"{pid}.json"
    path.write_text(json.dumps({"sessionId": sid, "procStart": proc_start}))
    return path


def _bare(pid: int, cwd: str = "/tmp/proj", starttime: str = "555") -> ProcCli:
    """A title-rewritten bare kimi process (no argv sid, no tmux metadata)."""
    return ProcCli(
        pid=pid,
        argv=("kimi-code",),
        starttime=starttime,
        cwd=cwd,
        comm="kimi-code",
        exe="/home/x/.kimi-code/bin/kimi",
    )


def _write_session(home, sid: str, work_dir: str = "/tmp/proj", mtime: float = 1000):
    import os

    session_dir = home / "sessions" / "wd_x_1" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    state = session_dir / "state.json"
    state.write_text(json.dumps({"title": "t", "workDir": work_dir}))
    os.utime(state, (mtime, mtime))
    with open(home / "session_index.jsonl", "a") as fh:
        fh.write(
            json.dumps(
                {
                    "sessionId": sid,
                    "sessionDir": str(session_dir),
                    "workDir": work_dir,
                }
            )
            + "\n"
        )


def _start_payload(sid: str = SID, event: str = "SessionStart") -> str:
    return json.dumps(
        {
            "hook_event_name": event,
            "session_id": sid,
            "cwd": "/tmp/proj",
            "client_type": "kimi_code_cli",
            "source": "startup",
        }
    )


# --- the hook command ----------------------------------------------------------


class TestRunHook:
    @pytest.fixture
    def hosting(self, monkeypatch):
        """The verified ancestry: hook(900) → sh(700) → kimi(800, start 555)."""
        monkeypatch.setattr(kimi_hook, "_kimi_pid", lambda: 800)
        monkeypatch.setattr(
            kimi_hook.proc,
            "read_proc_stat",
            lambda pid: kimi_hook.proc.ProcStatRead(
                pid=pid,
                state=kimi_hook.ProcReadState.AVAILABLE,
                path=f"/proc/{pid}/stat",
                starttime="555",
            ),
        )

    def test_session_start_writes_the_registry_entry(self, kimi_home, hosting):
        assert kimi_hook.run_hook(_start_payload()) == 0

        record = json.loads((kimi_home / "run" / "800.json").read_text())
        assert record == {"sessionId": SID, "procStart": "555"}

    def test_session_end_removes_the_entry(self, kimi_home, hosting):
        path = _registry_file(kimi_home, 800)

        assert kimi_hook.run_hook(_start_payload(event="SessionEnd")) == 0
        assert not path.exists()

    def test_session_end_without_an_entry_is_a_no_op(self, kimi_home, hosting):
        assert kimi_hook.run_hook(_start_payload(event="SessionEnd")) == 0

    def test_unknown_events_are_ignored(self, kimi_home, hosting):
        payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": SID})
        assert kimi_hook.run_hook(payload) == 0
        assert not (kimi_home / "run").exists()

    def test_malformed_payloads_are_rejected(self, kimi_home, hosting):
        assert kimi_hook.run_hook("not json") == 2
        assert kimi_hook.run_hook(json.dumps(["a list"])) == 2
        assert kimi_hook.run_hook(json.dumps({"hook_event_name": "SessionStart"})) == 2
        assert not (kimi_home / "run").exists()

    def test_unreadable_ancestry_binds_nothing(self, kimi_home, monkeypatch):
        monkeypatch.setattr(kimi_hook, "_kimi_pid", lambda: None)
        assert kimi_hook.run_hook(_start_payload()) == 3
        assert not (kimi_home / "run").exists()

    def test_pid_comes_from_the_nearest_grandparent(self, monkeypatch):
        reads = {
            700: (800, "111"),  # sh → kimi
            800: (1, "555"),
        }
        monkeypatch.setattr(kimi_hook.os, "getppid", lambda: 700)
        monkeypatch.setattr(
            kimi_hook.proc,
            "read_proc_stat",
            lambda pid: kimi_hook.proc.ProcStatRead(
                pid=pid,
                state=kimi_hook.ProcReadState.AVAILABLE,
                path=f"/proc/{pid}/stat",
                ppid=reads[pid][0],
                starttime=reads[pid][1],
            ),
        )
        assert kimi_hook._kimi_pid() == 800

    def test_pid_is_none_when_the_shell_is_gone(self, monkeypatch):
        monkeypatch.setattr(kimi_hook.os, "getppid", lambda: 700)
        monkeypatch.setattr(
            kimi_hook.proc,
            "read_proc_stat",
            lambda pid: kimi_hook.proc.ProcStatRead(
                pid=pid, state=kimi_hook.ProcReadState.GONE, path=f"/proc/{pid}/stat"
            ),
        )
        assert kimi_hook._kimi_pid() is None


def test_hook_command_routing(kimi_home, monkeypatch):
    monkeypatch.setattr(kimi_hook, "_kimi_pid", lambda: 800)
    monkeypatch.setattr(
        kimi_hook.proc,
        "read_proc_stat",
        lambda pid: kimi_hook.proc.ProcStatRead(
            pid=pid,
            state=kimi_hook.ProcReadState.AVAILABLE,
            path=f"/proc/{pid}/stat",
            starttime="555",
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_start_payload()))

    assert cmd_kimi_hook([]) == 0
    assert (kimi_home / "run" / "800.json").exists()
    assert cmd_kimi_hook(["stray"]) == 2


def test_hook_command_main_intercept_rejects_stray_args():
    assert main(["_kimi-hook", "stray"]) == 2


def test_hook_command_stays_out_of_the_public_parser(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    assert "_kimi-hook" not in capsys.readouterr().out


# --- the provider's verified join ----------------------------------------------


class TestRegistryBinding:
    def test_bare_tui_binds_via_the_registry(self, kimi_home):
        _write_session(kimi_home, SID)
        _registry_file(kimi_home, 800)
        inventory = ProcCliInventory(records=(_bare(800),))

        scan = KimiProvider().discover(inventory, cur=frozenset())

        assert scan.complete
        (row,) = scan.sessions
        assert row.alive and row.pid == 800 and row.proc_start == "555"

    def test_stale_starttime_never_binds(self, kimi_home):
        """A crashed session's leftover file: pid got reused by a NEW kimi."""
        _write_session(kimi_home, SID)
        _registry_file(kimi_home, 800, proc_start="999")
        inventory = ProcCliInventory(records=(_bare(800, starttime="555"),))

        scan = KimiProvider().discover(inventory, cur=frozenset())

        (row,) = scan.sessions
        assert not row.alive and row.pid is None

    def test_pid_absent_from_the_walk_never_binds(self, kimi_home):
        _write_session(kimi_home, SID)
        _registry_file(kimi_home, 800)

        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())

        (row,) = scan.sessions
        assert not row.alive

    def test_identity_mismatch_never_binds(self, kimi_home):
        _write_session(kimi_home, SID)
        _registry_file(kimi_home, 800)
        impostor = ProcCli(
            800, ("kimi-code",), "555", comm="other", exe="/usr/bin/other"
        )

        scan = KimiProvider().discover(
            ProcCliInventory(records=(impostor,)), cur=frozenset()
        )

        (row,) = scan.sessions
        assert not row.alive

    def test_malformed_entry_surfaces_as_an_issue(self, kimi_home):
        _write_session(kimi_home, SID)
        run = kimi_home / "run"
        run.mkdir()
        (run / "800.json").write_text('{"sessionId": 42}')

        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())

        assert not scan.complete
        assert scan.issues[0].source == "kimi sessions"

    def test_two_live_pids_claiming_one_sid_bind_nobody(self, kimi_home):
        """Double-attach is genuinely ambiguous for kill authority — fail
        closed rather than pick one (the metadata join's rule)."""
        _write_session(kimi_home, SID)
        _registry_file(kimi_home, 800)
        _registry_file(kimi_home, 900)
        inventory = ProcCliInventory(records=(_bare(800), _bare(900)))

        scan = KimiProvider().discover(inventory, cur=frozenset())

        (row,) = scan.sessions
        assert not row.alive and row.pid is None

    def test_registry_loses_to_intact_argv(self, kimi_home):
        """argv-exact keeps top priority (a resume argv observed before the
        title rewrite outranks the registry self-report)."""
        _write_session(kimi_home, SID)
        _registry_file(kimi_home, 800)
        argv_proc = ProcCli(900, ("kimi", "--session", SID), "777", cwd="/tmp/proj")
        inventory = ProcCliInventory(records=(argv_proc, _bare(800)))

        scan = KimiProvider().discover(inventory, cur=frozenset())

        (row,) = scan.sessions
        assert row.alive and row.pid == 900 and row.proc_start == "777"

    def test_registry_beats_dispatch_metadata(self, kimi_home):
        from cc_session_control.data.tmux_outcomes import PaneInventory, TmuxPane

        _write_session(kimi_home, SID)
        _registry_file(kimi_home, 800)
        inventory = ProcCliInventory(records=(_bare(800), _bare(900)))
        panes = PaneInventory((TmuxPane("proj:1", 900, SID, "kimi"),))

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert row.alive and row.pid == 800

    def test_missing_registry_dir_is_not_an_issue(self, kimi_home):
        _write_session(kimi_home, SID)

        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())

        assert scan.complete
        (row,) = scan.sessions
        assert not row.alive
