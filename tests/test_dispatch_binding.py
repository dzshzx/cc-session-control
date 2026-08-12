"""Late-sid dispatch binding (kimi new-session `@csctl_sid` backfill).

kimi registers a session (index entry + sid) only at its first prompt, so a
dispatched NEW window cannot declare `@csctl_sid` at spawn
(`test_tmux_metadata_binding.py` covers the spawn-time declaration). These
tests pin the backfill: the spawn command embeds the `_bind-window` watch
for late-sid providers, and the watch writes the option only off a unique,
snapshot-fresh index candidate with the pane process identity intact —
every doubt exits unbound.
"""

from __future__ import annotations

import json

import pytest

from cc_session_control.actions import dispatch_binding, session_ops
from cc_session_control.cli import build_parser, main
from cc_session_control.config import cfg
from cc_session_control.data.proc import PidProbe, ProcReadState, ProcStatRead
from cc_session_control.data.providers import kimi as kimi_mod
from cc_session_control.data.providers.kimi import KimiProvider
from cc_session_control.data.tmux_outcomes import window_option_result

SID_NEW = "session_00000000-0000-4000-8000-000000000001"
SID_NEW2 = "session_00000000-0000-4000-8000-000000000002"
SID_OLD = "session_00000000-0000-4000-8000-0000000000aa"


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    home = tmp_path / "kimi"
    home.mkdir()
    monkeypatch.setattr(cfg, "kimi_home", home)
    return home


def _append_index(home, sid: str, work_dir: str) -> None:
    with open(home / "session_index.jsonl", "a") as fh:
        fh.write(
            json.dumps(
                {
                    "sessionId": sid,
                    "sessionDir": str(home / "sessions" / "wd_x_1" / sid),
                    "workDir": work_dir,
                }
            )
            + "\n"
        )


def _alive(pid: int, starttime: str = "555") -> PidProbe:
    stat = ProcStatRead(
        pid=pid,
        state=ProcReadState.AVAILABLE,
        path=f"/proc/{pid}/stat",
        starttime=starttime,
    )
    return PidProbe(pid=pid, alive=True, stat=stat)


@pytest.fixture
def pane_env(monkeypatch):
    """The watcher's in-pane seams: $TMUX_PANE, window identity, live pane pid."""
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setattr(
        dispatch_binding.tmux, "pane_window_identity", lambda pane: ("@7", 800)
    )
    monkeypatch.setattr(
        dispatch_binding.proc, "probe_pid", lambda pid, start: _alive(pid)
    )


def _no_sleep(_seconds: float) -> None:
    return None


def _declare_recorder(declared: list) -> object:
    """Fake `declare_dispatch_sid` recording calls and reporting success."""

    def declare(window_id: str, sid: str):
        declared.append((window_id, sid))
        return window_option_result(window_id, 0, "")

    return declare


# --- spawn command composition -----------------------------------------------


class TestSpawnCommand:
    def test_plain_provider_command_is_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            dispatch_binding.shutil, "which", lambda name: "/usr/local/bin/csctl"
        )
        provider = session_ops.providers.get("claude")

        cmd = dispatch_binding.new_session_spawn_cmd("/tmp/proj with space", provider)

        assert cmd == "cd '/tmp/proj with space' && claude"

    def test_kimi_command_embeds_the_backfill_watch(self, monkeypatch):
        monkeypatch.setattr(
            dispatch_binding.shutil, "which", lambda name: "/usr/local/bin/csctl"
        )

        cmd = dispatch_binding.new_session_spawn_cmd("/tmp/proj", KimiProvider())

        assert cmd == (
            "/usr/local/bin/csctl _bind-window kimi /tmp/proj "
            ">/dev/null 2>&1 & cd /tmp/proj && exec kimi"
        )

    def test_kimi_command_without_csctl_on_path_stays_plain(self, monkeypatch):
        monkeypatch.setattr(dispatch_binding.shutil, "which", lambda name: None)

        cmd = dispatch_binding.new_session_spawn_cmd("/tmp/proj", KimiProvider())

        assert cmd == "cd /tmp/proj && kimi"


# --- the index helpers the watch diffs ----------------------------------------


class TestIndexHelpers:
    def test_missing_index_is_an_empty_snapshot(self, kimi_home):
        assert kimi_mod.index_sids() == frozenset()

    def test_index_sids_skips_torn_and_foreign_lines(self, kimi_home):
        with open(kimi_home / "session_index.jsonl", "w") as fh:
            fh.write('{"unrelated": true}\n')
            fh.write('{"sessionId": "session_a", "workDir": "/tmp/proj"}\n')
            fh.write('{"sessionId": "truncted\n')  # torn tail
        assert kimi_mod.index_sids() == frozenset({"session_a"})

    def test_unreadable_index_is_none_not_empty(self, kimi_home):
        (kimi_home / "session_index.jsonl").mkdir()  # open() → OSError
        assert kimi_mod.index_sids() is None
        assert kimi_mod.new_sids_since(frozenset(), "/tmp/proj") is None

    def test_new_sids_since_filters_prior_and_workdir(self, kimi_home):
        _append_index(kimi_home, SID_OLD, "/tmp/proj")
        _append_index(kimi_home, SID_NEW, "/tmp/proj")
        _append_index(kimi_home, SID_NEW2, "/tmp/other")

        assert kimi_mod.new_sids_since(frozenset({SID_OLD}), "/tmp/proj") == (SID_NEW,)


# --- the watch loop ------------------------------------------------------------


class TestRunBindingWatch:
    def test_binds_once_the_session_registers(self, kimi_home, pane_env, monkeypatch):
        declared = []
        monkeypatch.setattr(
            dispatch_binding.tmux, "declare_dispatch_sid", _declare_recorder(declared)
        )

        def sleep_then_register(_seconds: float) -> None:
            # Registration must land AFTER the watcher's snapshot loop
            # (first sleep), like kimi's first-prompt registration.
            sleep_then_register.calls += 1
            if sleep_then_register.calls == 2:
                _append_index(kimi_home, SID_NEW, "/tmp/proj")

        sleep_then_register.calls = 0

        code = dispatch_binding.run_binding_watch(
            "kimi", "/tmp/proj", interval=0, sleep=sleep_then_register
        )

        assert code == 0
        assert declared == [("@7", SID_NEW)]

    def test_several_new_sessions_fail_closed(self, kimi_home, pane_env, monkeypatch):
        declared = []
        monkeypatch.setattr(
            dispatch_binding.tmux,
            "declare_dispatch_sid",
            lambda window_id, sid: declared.append((window_id, sid)),
        )

        def sleep_then_register(_seconds: float) -> None:
            sleep_then_register.calls += 1
            if sleep_then_register.calls == 2:
                _append_index(kimi_home, SID_NEW, "/tmp/proj")
                _append_index(kimi_home, SID_NEW2, "/tmp/proj")

        sleep_then_register.calls = 0

        code = dispatch_binding.run_binding_watch(
            "kimi", "/tmp/proj", interval=0, sleep=sleep_then_register
        )

        assert code == 2
        assert declared == []

    def test_pre_existing_sessions_are_never_candidates(
        self, kimi_home, pane_env, monkeypatch
    ):
        _append_index(kimi_home, SID_OLD, "/tmp/proj")
        ticks = iter(range(100))
        declared = []
        monkeypatch.setattr(
            dispatch_binding.tmux,
            "declare_dispatch_sid",
            lambda window_id, sid: declared.append((window_id, sid)),
        )

        code = dispatch_binding.run_binding_watch(
            "kimi",
            "/tmp/proj",
            interval=0,
            horizon=5.0,
            sleep=_no_sleep,
            monotonic=lambda: float(next(ticks)),
        )

        assert code == 7
        assert declared == []

    def test_foreign_workdir_registration_is_not_a_candidate(
        self, kimi_home, pane_env, monkeypatch
    ):
        _append_index(kimi_home, SID_NEW, "/tmp/other")
        ticks = iter(range(100))

        code = dispatch_binding.run_binding_watch(
            "kimi",
            "/tmp/proj",
            interval=0,
            horizon=5.0,
            sleep=_no_sleep,
            monotonic=lambda: float(next(ticks)),
        )

        assert code == 7

    def test_snapshot_failure_retries_until_readable(
        self, kimi_home, pane_env, monkeypatch
    ):
        declared = []
        monkeypatch.setattr(
            dispatch_binding.tmux, "declare_dispatch_sid", _declare_recorder(declared)
        )
        snapshots = iter([None, frozenset()])
        monkeypatch.setattr(
            dispatch_binding.kimi, "index_sids", lambda: next(snapshots)
        )
        monkeypatch.setattr(
            dispatch_binding.kimi,
            "new_sids_since",
            lambda prior, directory: (SID_NEW,),
        )

        code = dispatch_binding.run_binding_watch(
            "kimi", "/tmp/proj", interval=0, sleep=_no_sleep
        )

        assert code == 0
        assert declared == [("@7", SID_NEW)]

    def test_changed_pane_process_never_binds(self, kimi_home, pane_env, monkeypatch):
        probes = iter([_alive(800), PidProbe(pid=800, alive=False)])
        monkeypatch.setattr(
            dispatch_binding.proc, "probe_pid", lambda pid, start: next(probes)
        )
        declared = []
        monkeypatch.setattr(
            dispatch_binding.tmux,
            "declare_dispatch_sid",
            lambda window_id, sid: declared.append((window_id, sid)),
        )

        def sleep_then_register(_seconds: float) -> None:
            sleep_then_register.calls += 1
            if sleep_then_register.calls == 2:
                _append_index(kimi_home, SID_NEW, "/tmp/proj")

        sleep_then_register.calls = 0

        code = dispatch_binding.run_binding_watch(
            "kimi", "/tmp/proj", interval=0, sleep=sleep_then_register
        )

        assert code == 4
        assert declared == []

    def test_failed_option_write_keeps_exit_evidence(
        self, kimi_home, pane_env, monkeypatch
    ):
        monkeypatch.setattr(
            dispatch_binding.tmux,
            "declare_dispatch_sid",
            lambda window_id, sid: window_option_result(window_id, 1, "boom"),
        )
        monkeypatch.setattr(dispatch_binding.kimi, "index_sids", lambda: frozenset())
        monkeypatch.setattr(
            dispatch_binding.kimi,
            "new_sids_since",
            lambda prior, directory: (SID_NEW,),
        )

        code = dispatch_binding.run_binding_watch(
            "kimi", "/tmp/proj", interval=0, sleep=_no_sleep
        )

        assert code == 5

    def test_other_providers_refuse(self):
        assert (
            dispatch_binding.run_binding_watch(
                "codex", "/tmp/proj", interval=0, sleep=_no_sleep
            )
            == 6
        )

    def test_without_pane_env_there_is_no_window(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        assert (
            dispatch_binding.run_binding_watch(
                "kimi", "/tmp/proj", interval=0, sleep=_no_sleep
            )
            == 3
        )

    def test_lost_window_identity_exits_unbound(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%7")
        monkeypatch.setattr(
            dispatch_binding.tmux, "pane_window_identity", lambda pane: None
        )
        assert (
            dispatch_binding.run_binding_watch(
                "kimi", "/tmp/proj", interval=0, sleep=_no_sleep
            )
            == 3
        )

    def test_unverifiable_pane_process_exits_unbound(self, monkeypatch):
        monkeypatch.setenv("TMUX_PANE", "%7")
        monkeypatch.setattr(
            dispatch_binding.tmux, "pane_window_identity", lambda pane: ("@7", 800)
        )
        monkeypatch.setattr(
            dispatch_binding.proc,
            "probe_pid",
            lambda pid, start: PidProbe(pid=pid, alive=None),
        )
        assert (
            dispatch_binding.run_binding_watch(
                "kimi", "/tmp/proj", interval=0, sleep=_no_sleep
            )
            == 4
        )


# --- CLI wiring -----------------------------------------------------------------


def test_bind_window_main_intercepts_the_internal_command():
    assert main(["_bind-window", "codex", "/tmp/proj"]) == 6


def test_bind_window_rejects_a_malformed_invocation():
    assert main(["_bind-window", "kimi"]) == 2


def test_bind_window_stays_out_of_the_public_parser(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    assert "_bind-window" not in capsys.readouterr().out
