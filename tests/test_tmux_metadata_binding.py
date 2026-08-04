"""tmux window-metadata liveness binding (candidate C1).

Kimi Code 0.31.1 rewrites its own process title at runtime: an active
dispatched session's `/proc/<pid>/cmdline` collapses to `kimi-code` plus
padding, destroying the `--session <sid>` argv evidence ADR-0005's
argv-exact binding keys on. These tests cover the three-layer fix: every
csctl tmux dispatch declares `@csctl_sid`/`@csctl_provider` window options
at spawn; discovery joins a declaring pane to the pane process whose
exe/comm/argv0 matches the provider's process-identity set (argv bindings
keep priority; every doubt binds nothing); and the unbound-live hint
recognizes title-rewritten bare processes by the same identity set.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from factories import make_session

from cc_session_control.actions import session_ops
from cc_session_control.config import cfg
from cc_session_control.data import providers, tmux
from cc_session_control.data.proc import AncestorProbe, ProcCli, ProcCliInventory
from cc_session_control.data.providers import kimi as kimi_mod
from cc_session_control.data.providers.kimi import KimiProvider
from cc_session_control.data.tmux_outcomes import (
    PaneInventory,
    ResidencyIssue,
    TmuxPane,
)

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"
UUID2 = "019fc790-3126-7601-9e9b-fd378524ada8"
KIMI_SID = f"session_{UUID1}"
KIMI_SID2 = f"session_{UUID2}"


# --- spawn side: dispatch declares its identity on the window ---------------


def _spawn_run(calls):
    """Fake subprocess.run: existing session, new-window prints its target."""

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1] == "new-window":
            return subprocess.CompletedProcess(argv, 0, stdout="proj:3\n", stderr="")
        if argv[1] == "set-option":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)

    return run


class TestSpawnDeclaresDispatchMetadata:
    def test_run_in_tmux_writes_provider_and_sid_options(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(tmux.subprocess, "run", _spawn_run(calls))

        result = tmux.run_in_tmux_result(
            "proj", "km-019fc784", "cmd", sid=KIMI_SID, provider="kimi"
        )

        assert result.success and result.target == "proj:3"
        option_calls = [argv for argv in calls if argv[1] == "set-option"]
        assert option_calls == [
            ["tmux", "set-option", "-w", "-t", "proj:3", "@csctl_provider", "kimi"],
            ["tmux", "set-option", "-w", "-t", "proj:3", "@csctl_sid", KIMI_SID],
        ]

    def test_metadata_write_failure_never_fails_the_spawn(self, monkeypatch):
        def run(argv, **_kwargs):
            if argv[1] == "has-session":
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[1] == "new-window":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="proj:3\n", stderr=""
                )
            if argv[1] == "set-option":
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="bad option\n"
                )
            raise AssertionError(argv)

        monkeypatch.setattr(tmux.subprocess, "run", run)

        result = tmux.run_in_tmux_result(
            "proj", "km-019fc784", "cmd", sid=KIMI_SID, provider="kimi"
        )

        assert result.success and result.target == "proj:3"
        assert "@csctl_provider" in result.detail
        assert "bad option" in result.detail

    def test_without_metadata_kwargs_no_option_is_written(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(tmux.subprocess, "run", _spawn_run(calls))

        result = tmux.run_in_tmux_result("proj", "claude", "cmd")

        assert result.success
        assert all(argv[1] != "set-option" for argv in calls)

    def test_failed_spawn_writes_no_metadata(self, monkeypatch):
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "has-session":
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="boom\n")

        monkeypatch.setattr(tmux.subprocess, "run", run)

        result = tmux.run_in_tmux_result("proj", "w", "cmd", sid="x", provider="kimi")

        assert not result.success
        assert all(argv[1] != "set-option" for argv in calls)


class TestSessionOpsPassMetadata:
    """The resume family and the launcher declare identity through the one
    tmux seam; a fork window hosts a NEW unknown sid, so it declares only
    the provider (binding the parent sid would mint a wrong kill target)."""

    def _capture(self, monkeypatch):
        seen: list[dict] = []

        def fake_run(session, window, cmd, sid="", provider=""):
            seen.append(
                {"session": session, "window": window, "sid": sid, "provider": provider}
            )
            return tmux.TmuxWriteResult(
                tmux.TmuxWriteStage.NEW_WINDOW,
                tmux.TmuxWriteState.SUCCEEDED,
                target="proj:5",
            )

        monkeypatch.setattr(session_ops.tmux, "run_in_tmux_result", fake_run)
        return seen

    def test_dead_resume_declares_sid_and_provider(self, monkeypatch):
        seen = self._capture(monkeypatch)
        s = make_session(sid=KIMI_SID, provider="kimi", alive=False, cwd="/tmp")

        outcome = session_ops.do_tmux_resume_result(s)

        assert outcome.target == "proj:5"
        assert seen == [
            {
                "session": "tmp",
                "window": "km-019fc784",
                "sid": KIMI_SID,
                "provider": "kimi",
            }
        ]

    def test_fork_declares_provider_only(self, monkeypatch):
        seen = self._capture(monkeypatch)
        s = make_session(sid=UUID1, provider="codex", alive=False, cwd="/tmp")

        outcome = session_ops.do_tmux_resume_result(s, fork=True)

        assert outcome.target == "proj:5"
        assert seen[0]["sid"] == ""
        assert seen[0]["provider"] == "codex"

    def test_launcher_new_session_declares_provider_only(self, monkeypatch, tmp_path):
        seen = self._capture(monkeypatch)

        result = session_ops.do_tmux_new_result(str(tmp_path), "kimi")

        assert result.success
        assert seen[0]["sid"] == ""
        assert seen[0]["provider"] == "kimi"


# --- read side: the one pane walk carries the metadata ----------------------


class TestPaneInventoryCarriesMetadata:
    def test_parses_metadata_fields(self, monkeypatch):
        monkeypatch.setattr(
            tmux.subprocess,
            "run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    f"proj:1\t800\t{KIMI_SID}\tkimi\n"
                    "other:2\t900\t\t\n"  # plain window — no options set
                ),
                stderr="",
            ),
        )

        inventory = tmux.list_panes_inventory()

        assert inventory.complete
        assert inventory.records == (
            TmuxPane("proj:1", 800, KIMI_SID, "kimi"),
            TmuxPane("other:2", 900, "", ""),
        )

    def test_malformed_row_stays_an_issue(self, monkeypatch):
        monkeypatch.setattr(
            tmux.subprocess,
            "run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(
                argv,
                0,
                stdout="proj:1\tnot-a-pid\t\t\n",
                stderr="",
            ),
        )

        inventory = tmux.list_panes_inventory()

        assert not inventory.complete
        assert inventory.records == ()


# --- kimi process identity + hint hardening ---------------------------------


def _rewritten(pid: int, cwd: str = "", starttime: str = "555") -> ProcCli:
    """The observed title-rewritten kimi form (2026-08-04, kimi 0.31.1)."""
    return ProcCli(
        pid=pid,
        argv=("kimi-code",),
        starttime=starttime,
        cwd=cwd,
        comm="kimi-code",
        exe="/home/x/.kimi-code/bin/kimi",
    )


class TestKimiProcessIdentity:
    def test_classic_argv0_matches(self):
        assert kimi_mod.is_tui_process(ProcCli(1, ("kimi",), "1"))

    def test_title_rewritten_form_matches_via_comm_and_exe(self):
        assert kimi_mod.is_tui_process(_rewritten(1))
        assert kimi_mod.is_tui_process(
            ProcCli(1, ("kimi-code",), "1", comm="kimi-code", exe="")
        )
        assert kimi_mod.is_tui_process(
            ProcCli(1, ("kimi-code",), "1", comm="", exe="/opt/kimi/bin/kimi")
        )

    def test_unrelated_identity_never_matches(self):
        assert not kimi_mod.is_tui_process(
            ProcCli(1, ("kimi-code",), "1", comm="other", exe="/usr/bin/other")
        )
        assert not kimi_mod.is_tui_process(ProcCli(1, ("codex",), "1"))

    def test_daemon_shapes_never_match_even_with_identity(self):
        assert not kimi_mod.is_tui_process(
            ProcCli(1, ("kimi", "web"), "1", exe="/opt/kimi/bin/kimi")
        )
        assert not kimi_mod.is_tui_process(
            ProcCli(1, ("kimi", "-p", "x"), "1", comm="kimi-code")
        )


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    home = tmp_path / "kimi"
    home.mkdir()
    monkeypatch.setattr(cfg, "kimi_home", home)
    return home


def _write_kimi_session(home, sid: str, work_dir: str, mtime: float) -> None:
    import os

    session_dir = home / "sessions" / "wd_x_123" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    state = session_dir / "state.json"
    state.write_text(json.dumps({"title": "t", "workDir": work_dir}))
    os.utime(state, (mtime, mtime))
    with open(home / "session_index.jsonl", "a") as fh:
        fh.write(json.dumps({"sessionId": sid, "sessionDir": str(session_dir)}) + "\n")


def _panes(*records: TmuxPane, issues=()) -> PaneInventory:
    return PaneInventory(tuple(records), tuple(issues))


class TestKimiMetadataBinding:
    def test_title_rewritten_dispatched_session_binds_alive(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        inventory = ProcCliInventory(records=(_rewritten(800, "/tmp/proj"),))
        panes = _panes(TmuxPane("proj:1", 800, KIMI_SID, "kimi"))

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        assert scan.complete
        (row,) = scan.sessions
        assert row.alive and row.pid == 800 and row.proc_start == "555"
        assert not row.current
        assert not row.unbound_live_hint  # bound processes are accounted for

    def test_pane_shell_wrapper_binds_unique_tui_descendant(
        self, kimi_home, monkeypatch
    ):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        inventory = ProcCliInventory(records=(_rewritten(800, "/tmp/proj"),))
        panes = _panes(TmuxPane("proj:1", 700, KIMI_SID, "kimi"))
        monkeypatch.setattr(
            kimi_mod.proc,
            "probe_ancestors",
            lambda pid: AncestorProbe(frozenset({pid, 700, 1})),
        )

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert row.alive and row.pid == 800

    def test_identity_mismatch_never_binds(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        impostor = ProcCli(
            800, ("kimi-code",), "555", comm="other", exe="/usr/bin/other"
        )
        panes = _panes(TmuxPane("proj:1", 800, KIMI_SID, "kimi"))

        scan = KimiProvider().discover(
            ProcCliInventory(records=(impostor,)), cur=frozenset(), panes=panes
        )

        (row,) = scan.sessions
        assert not row.alive and row.pid is None

    def test_missing_sid_option_keeps_dead_row_with_hint(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        inventory = ProcCliInventory(records=(_rewritten(800, "/tmp/proj"),))
        panes = _panes(TmuxPane("proj:1", 800, "", "kimi"))

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert not row.alive
        assert row.unbound_live_hint  # identity-hardened hint still fires

    def test_incomplete_pane_inventory_never_binds(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        inventory = ProcCliInventory(records=(_rewritten(800, "/tmp/proj"),))
        panes = _panes(
            TmuxPane("proj:1", 800, KIMI_SID, "kimi"),
            issues=(ResidencyIssue("tmux list-panes", None, "timed out"),),
        )

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert not row.alive

    def test_vanished_pane_process_never_binds(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        panes = _panes(TmuxPane("proj:1", 999, KIMI_SID, "kimi"))

        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert not row.alive

    def test_foreign_provider_claim_never_binds(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        inventory = ProcCliInventory(records=(_rewritten(800, "/tmp/proj"),))
        panes = _panes(TmuxPane("proj:1", 800, KIMI_SID, "codex"))

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert not row.alive

    def test_ambiguous_claims_for_one_sid_bind_nothing(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        inventory = ProcCliInventory(
            records=(_rewritten(800, "/tmp/proj"), _rewritten(900, "/tmp/proj")),
        )
        panes = _panes(
            TmuxPane("proj:1", 800, KIMI_SID, "kimi"),
            TmuxPane("proj:2", 900, KIMI_SID, "kimi"),
        )

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert not row.alive

    def test_argv_binding_keeps_priority_over_metadata(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        argv_proc = ProcCli(
            900, ("kimi", "--session", KIMI_SID), "777", cwd="/tmp/proj"
        )
        inventory = ProcCliInventory(records=(argv_proc, _rewritten(800, "/tmp/proj")))
        panes = _panes(TmuxPane("proj:1", 800, KIMI_SID, "kimi"))

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert row.alive and row.pid == 900 and row.proc_start == "777"

    def test_metadata_bound_process_no_longer_hints_sibling_rows(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        _write_kimi_session(kimi_home, KIMI_SID2, "/tmp/proj", 2000)
        inventory = ProcCliInventory(records=(_rewritten(800, "/tmp/proj"),))
        panes = _panes(TmuxPane("proj:1", 800, KIMI_SID, "kimi"))

        scan = KimiProvider().discover(inventory, cur=frozenset(), panes=panes)

        by_sid = {row.sid: row for row in scan.sessions}
        assert by_sid[KIMI_SID].alive
        # The one process is bound to KIMI_SID — the newer sibling must NOT
        # inherit a stale "possibly held" hint from it.
        assert not by_sid[KIMI_SID2].unbound_live_hint
        assert not by_sid[KIMI_SID2].alive

    def test_current_ancestor_binding_marks_current(self, kimi_home):
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        inventory = ProcCliInventory(records=(_rewritten(800, "/tmp/proj"),))
        panes = _panes(TmuxPane("proj:1", 800, KIMI_SID, "kimi"))

        scan = KimiProvider().discover(inventory, cur=frozenset({800}), panes=panes)

        (row,) = scan.sessions
        assert row.alive and row.current


# --- codex: intact argv keeps priority; metadata is a pure supplement -------


class TestCodexUnchangedWithIntactArgv:
    @pytest.fixture
    def codex_home(self, tmp_path, monkeypatch):
        home = tmp_path / "codex"
        (home / "sessions").mkdir(parents=True)
        monkeypatch.setattr(cfg, "codex_home", home)
        return home

    def _write_rollout(self, home, sid: str) -> None:
        directory = home / "sessions" / "2026" / "08" / "01"
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": sid,
            "session_id": sid,
            "cwd": "/tmp/proj",
            "thread_source": "user",
        }
        record = {"timestamp": "t", "type": "session_meta", "payload": payload}
        (directory / f"rollout-a-{sid}.jsonl").write_text(json.dumps(record) + "\n")

    def test_argv_bound_row_ignores_conflicting_metadata(self, codex_home):
        from cc_session_control.data.providers.codex import CodexProvider

        self._write_rollout(codex_home, UUID1)
        argv_proc = ProcCli(42, ("codex", "resume", UUID1), "777", cwd="/tmp/proj")
        inventory = ProcCliInventory(records=(argv_proc,))
        panes = _panes(TmuxPane("proj:1", 99, UUID1, "codex"))

        scan = CodexProvider().discover(inventory, cur=frozenset(), panes=panes)

        (row,) = scan.sessions
        assert row.alive and row.pid == 42 and row.proc_start == "777"

    def test_no_panes_matches_pre_c1_behavior(self, codex_home):
        from cc_session_control.data.providers.codex import CodexProvider

        self._write_rollout(codex_home, UUID1)
        argv_proc = ProcCli(42, ("codex", "resume", UUID1), "777", cwd="/tmp/proj")
        inventory = ProcCliInventory(records=(argv_proc,))

        with_panes = CodexProvider().discover(
            inventory, cur=frozenset(), panes=_panes()
        )
        without = CodexProvider().discover(inventory, cur=frozenset())

        assert with_panes.sessions == without.sessions


# --- pipeline: one pane walk per pass; execution-time symmetry --------------


class TestScanNonClaudePaneEvidence:
    def test_one_pane_walk_serves_all_providers(self, kimi_home, monkeypatch):
        monkeypatch.setattr(cfg, "providers", ("claude", "kimi"))
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(
                records=(_rewritten(800, "/tmp/proj"),),
            ),
        )
        pane_calls = {"n": 0}

        def fake_panes():
            pane_calls["n"] += 1
            return _panes(TmuxPane("proj:1", 800, KIMI_SID, "kimi"))

        monkeypatch.setattr(providers.tmux, "list_panes_inventory", fake_panes)

        rows, issues = providers.scan_non_claude(frozenset())

        assert pane_calls["n"] == 1
        (row,) = rows
        assert row.alive and row.pid == 800
        assert issues == ()

    def test_empty_inventory_never_touches_tmux(self, kimi_home, monkeypatch):
        monkeypatch.setattr(cfg, "providers", ("claude", "kimi"))
        _write_kimi_session(kimi_home, KIMI_SID, "/tmp/proj", 1000)
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(),
        )

        def forbidden():
            raise AssertionError("no CLI processes — the pane walk must not run")

        monkeypatch.setattr(providers.tmux, "list_panes_inventory", forbidden)

        rows, issues = providers.scan_non_claude(frozenset())

        assert not rows[0].alive


class TestExecutionTimeMetadataResolution:
    """`resolve_argv_execution` re-gathers BOTH evidence sources fresh —
    the destructive-resolver symmetry the kill defenses key on."""

    def _arrange(self, kimi_home, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg, "providers", ("claude", "kimi"))
        cwd = tmp_path / "proj"
        cwd.mkdir(exist_ok=True)
        _write_kimi_session(kimi_home, KIMI_SID, str(cwd), 1000)
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(
                records=(_rewritten(800, str(cwd)),),
            ),
        )
        return cwd

    def test_rewritten_kimi_resolves_fresh_whole_session(
        self, kimi_home, monkeypatch, tmp_path
    ):
        self._arrange(kimi_home, monkeypatch, tmp_path)
        monkeypatch.setattr(
            providers.tmux,
            "list_panes_inventory",
            lambda: _panes(TmuxPane("proj:1", 800, KIMI_SID, "kimi")),
        )
        from cc_session_control.data.tmux_outcomes import ResidencyInventory

        monkeypatch.setattr(
            providers.tmux,
            "residency_inventory",
            lambda pids: ResidencyInventory(targets={800: "proj:1"}),
        )

        resolution = providers.resolve_argv_execution("kimi", KIMI_SID)

        assert resolution.success
        assert resolution.session is not None
        # The kill path gets a FRESH pid + proc_start — take_over_result's
        # probe recheck can defeat pid reuse on exactly these values.
        assert resolution.session.pid == 800
        assert resolution.session.proc_start == "555"
        assert resolution.session.tmux_target == "proj:1"

    def test_incomplete_pane_evidence_resolves_row_as_dead(
        self, kimi_home, monkeypatch, tmp_path
    ):
        """Fail closed for the KILL: no binding without complete pane
        evidence, so the resolver hands back a dead row (plain resume, no
        SIGTERM) rather than a guessed pid."""
        self._arrange(kimi_home, monkeypatch, tmp_path)
        monkeypatch.setattr(
            providers.tmux,
            "list_panes_inventory",
            lambda: _panes(
                TmuxPane("proj:1", 800, KIMI_SID, "kimi"),
                issues=(ResidencyIssue("tmux list-panes", None, "timed out"),),
            ),
        )

        resolution = providers.resolve_argv_execution("kimi", KIMI_SID)

        assert resolution.success
        assert resolution.session is not None
        assert not resolution.session.alive and resolution.session.pid is None

    def test_vanished_binding_and_missing_sid_fail_closed(
        self, kimi_home, monkeypatch, tmp_path
    ):
        self._arrange(kimi_home, monkeypatch, tmp_path)
        monkeypatch.setattr(
            providers.tmux,
            "list_panes_inventory",
            lambda: _panes(),  # dispatched window is gone
        )

        resolution = providers.resolve_argv_execution("kimi", KIMI_SID)
        assert resolution.success and not resolution.session.alive

        missing = providers.resolve_argv_execution("kimi", "session_nope")
        assert not missing.success
        assert "missing session id" in missing.detail

    def test_current_metadata_bound_session_refused(
        self, kimi_home, monkeypatch, tmp_path
    ):
        import os

        cwd = self._arrange(kimi_home, monkeypatch, tmp_path)
        my_pid = os.getpid()
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(
                records=(_rewritten(my_pid, str(cwd)),),
            ),
        )
        monkeypatch.setattr(
            providers.tmux,
            "list_panes_inventory",
            lambda: _panes(TmuxPane("proj:1", my_pid, KIMI_SID, "kimi")),
        )

        resolution = providers.resolve_argv_execution("kimi", KIMI_SID)

        assert not resolution.success
        assert "current session" in resolution.detail
