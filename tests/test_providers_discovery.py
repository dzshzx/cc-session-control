"""Codex/kimi disk discovery + argv-exact liveness (ADR-0005)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cc_session_control.actions import resume_list
from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.proc import ProcCli, ProcCliInventory
from cc_session_control.data.providers import kimi as kimi_mod
from cc_session_control.data.providers.codex import CodexProvider, extract_sid
from cc_session_control.data.providers.kimi import KimiProvider

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"
UUID2 = "019fc790-3126-7601-9e9b-fd378524ada8"


def _proc(pid: int, *argv: str, starttime: str = "100") -> ProcCli:
    return ProcCli(pid=pid, argv=tuple(argv), starttime=starttime)


class TestCodexExtract:
    def test_resume_matches(self):
        assert extract_sid(("codex", "resume", UUID1)) == UUID1
        assert extract_sid(("/usr/bin/codex", "resume", UUID1)) == UUID1

    def test_fork_never_binds_the_parent_sid(self):
        # `codex fork <sid>` is minting a NEW session; binding the parent sid
        # to the fork's pid would make the parent a wrong takeover target.
        assert extract_sid(("codex", "fork", UUID1)) is None

    def test_bare_and_daemon_argvs_never_match(self):
        assert extract_sid(("codex",)) is None
        assert extract_sid(("codex", "resume")) is None
        assert extract_sid(("codex", "app-server", "proxy")) is None
        assert extract_sid(("codex", "-c", "features.x=true", "app-server")) is None
        assert extract_sid(("kimi", "resume", UUID1)) is None

    def test_uuid_is_required_not_any_token(self):
        assert extract_sid(("codex", "resume", "not-a-uuid")) is None


class TestKimiExtract:
    def test_session_flag_matches(self):
        sid = f"session_{UUID1}"
        assert kimi_mod.extract_sid(("kimi", "--session", sid)) == sid
        assert kimi_mod.extract_sid(("kimi", "-S", sid)) == sid
        assert kimi_mod.extract_sid(("kimi", f"--session={sid}")) == sid

    def test_bare_and_flagless_never_match(self):
        assert kimi_mod.extract_sid(("kimi",)) is None
        assert kimi_mod.extract_sid(("kimi", "--continue")) is None
        assert kimi_mod.extract_sid(("kimi", "--session", "--yolo")) is None
        assert kimi_mod.extract_sid(("codex", "--session", "x")) is None


def _write_rollout(root, day: str, name: str, payload: dict) -> None:
    directory = root / "sessions" / "2026" / "08" / day
    directory.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": "t", "type": "session_meta", "payload": payload}
    (directory / name).write_text(json.dumps(record) + "\n")


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


class TestCodexDiscover:
    def test_projects_rows_with_index_labels_and_liveness(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"rollout-a-{UUID1}.jsonl",
            {
                "id": UUID1,
                "session_id": UUID1,
                "cwd": "/tmp/proj",
                "originator": "codex_cli_rs",
                "thread_source": "user",
            },
        )
        (codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": UUID1, "thread_name": "调研任务"}) + "\n"
        )
        inventory = ProcCliInventory(
            records=(_proc(42, "codex", "resume", UUID1, starttime="777"),),
        )
        scan = CodexProvider().discover(inventory, cur=frozenset())
        assert scan.complete
        (row,) = scan.sessions
        assert row.provider == "codex"
        assert row.sid == UUID1
        assert row.label == "调研任务"
        assert row.cwd == "/tmp/proj"
        assert row.alive and row.pid == 42 and row.proc_start == "777"
        assert not row.current

    def test_subagent_rollouts_are_skipped(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"rollout-b-{UUID2}.jsonl",
            {
                "id": UUID2,
                "session_id": UUID1,
                "cwd": "/tmp/proj",
                "thread_source": "subagent",
            },
        )
        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
        assert scan.sessions == ()

    def test_codex_exec_maps_to_sdk_source(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"rollout-c-{UUID1}.jsonl",
            {
                "id": UUID1,
                "session_id": UUID1,
                "cwd": "/tmp",
                "originator": "codex_exec",
                "thread_source": "user",
            },
        )
        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert row.source == "sdk"
        assert row.bridge_or_sdk

    def test_duplicate_sid_keeps_newest_rollout(self, codex_home):
        import os

        _write_rollout(
            codex_home,
            "01",
            f"rollout-old-{UUID1}.jsonl",
            {"id": UUID1, "session_id": UUID1, "cwd": "/old", "thread_source": "user"},
        )
        _write_rollout(
            codex_home,
            "02",
            f"rollout-new-{UUID1}.jsonl",
            {"id": UUID1, "session_id": UUID1, "cwd": "/new", "thread_source": "user"},
        )
        old = (
            codex_home
            / "sessions"
            / "2026"
            / "08"
            / "01"
            / f"rollout-old-{UUID1}.jsonl"
        )
        new = (
            codex_home
            / "sessions"
            / "2026"
            / "08"
            / "02"
            / f"rollout-new-{UUID1}.jsonl"
        )
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert row.cwd == "/new" and row.mtime == 2000

    def test_current_pid_marks_current(self, codex_home):
        _write_rollout(
            codex_home,
            "01",
            f"rollout-d-{UUID1}.jsonl",
            {"id": UUID1, "session_id": UUID1, "cwd": "/tmp", "thread_source": "user"},
        )
        inventory = ProcCliInventory(
            records=(_proc(42, "codex", "resume", UUID1),),
        )
        scan = CodexProvider().discover(inventory, cur=frozenset({42}))
        (row,) = scan.sessions
        assert row.current


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    home = tmp_path / "kimi"
    home.mkdir()
    monkeypatch.setattr(cfg, "kimi_home", home)
    return home


def _write_kimi_session(home, sid: str, state: dict | None) -> str:
    session_dir = home / "sessions" / "wd_x_123" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        (session_dir / "state.json").write_text(json.dumps(state))
    with open(home / "session_index.jsonl", "a") as fh:
        fh.write(
            json.dumps(
                {
                    "sessionId": sid,
                    "sessionDir": str(session_dir),
                    "workDir": "/tmp/index-dir",
                }
            )
            + "\n"
        )
    return str(session_dir)


class TestKimiDiscover:
    def test_projects_rows_from_index_and_state(self, kimi_home):
        sid = f"session_{UUID1}"
        _write_kimi_session(
            kimi_home,
            sid,
            {
                "title": "修复缓存",
                "lastPrompt": "继续",
                "workDir": "/tmp/state-dir",
            },
        )
        inventory = ProcCliInventory(
            records=(_proc(7, "kimi", "--session", sid, starttime="55"),),
        )
        scan = KimiProvider().discover(inventory, cur=frozenset())
        (row,) = scan.sessions
        assert row.provider == "kimi"
        assert row.sid == sid
        assert row.label == "修复缓存"
        assert row.cwd == "/tmp/state-dir"  # state.json wins over the index
        assert row.alive and row.pid == 7 and row.proc_start == "55"

    def test_missing_state_degrades_to_index_fields(self, kimi_home):
        sid = f"session_{UUID2}"
        _write_kimi_session(kimi_home, sid, None)
        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert row.label == "(untitled)"
        assert row.cwd == "/tmp/index-dir"
        assert not row.alive

    def test_last_prompt_is_the_title_fallback(self, kimi_home):
        sid = f"session_{UUID1}"
        _write_kimi_session(kimi_home, sid, {"lastPrompt": "建仓库"})
        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert row.label == "建仓库"

    def test_no_index_means_no_rows(self, kimi_home):
        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())
        assert scan.sessions == () and scan.complete

    def test_file_targets_the_real_conversation_not_state_json(self, kimi_home):
        # state.json only carries title/lastPrompt/workDir metadata — the
        # actual conversation content csctl needs for body search lives in
        # agents/main/wire.jsonl.
        sid = f"session_{UUID1}"
        session_dir = Path(
            _write_kimi_session(kimi_home, sid, {"title": "标题"})
        )
        wire_dir = session_dir / "agents" / "main"
        wire_dir.mkdir(parents=True)
        (wire_dir / "wire.jsonl").write_text(
            '{"role": "user", "text": "discuss ZEBRA-CROSSING-TOKEN here"}\n'
        )
        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        assert row.file == str(wire_dir / "wire.jsonl")
        # mtime keeps coming from state.json's stat, not the wire log's.
        assert row.mtime == os.stat(session_dir / "state.json").st_mtime

    def test_body_search_finds_a_word_only_present_in_wire_jsonl(self, kimi_home):
        sid = f"session_{UUID2}"
        session_dir = Path(
            _write_kimi_session(kimi_home, sid, {"title": "unrelated title"})
        )
        wire_dir = session_dir / "agents" / "main"
        wire_dir.mkdir(parents=True)
        (wire_dir / "wire.jsonl").write_text(
            '{"role": "assistant", "text": "the fix is FLUORESCENT-NARWHAL"}\n'
        )
        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())
        (row,) = scan.sessions
        # Not in sid/cwd/label metadata, so this only matches via body fallback.
        assert "fluorescent-narwhal" not in row.label.lower()
        assert resume_list.keyword_matches(row, "fluorescent-narwhal")
        assert not resume_list.keyword_matches(row, "no-such-word-anywhere")


class TestScanNonClaude:
    def test_inactive_providers_scan_nothing(self, monkeypatch):
        monkeypatch.setattr(cfg, "providers", ("claude",))
        rows, issues = providers.scan_non_claude(frozenset())
        assert rows == () and issues == ()

    def test_one_walk_feeds_every_provider(
        self,
        monkeypatch,
        codex_home,
        kimi_home,
    ):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex", "kimi"))
        _write_rollout(
            codex_home,
            "01",
            f"rollout-a-{UUID1}.jsonl",
            {"id": UUID1, "session_id": UUID1, "cwd": "/tmp", "thread_source": "user"},
        )
        kimi_sid = f"session_{UUID2}"
        _write_kimi_session(kimi_home, kimi_sid, {"title": "t"})
        walks: list[frozenset[str]] = []

        def fake_walk(basenames):
            walks.append(basenames)
            return ProcCliInventory()

        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            fake_walk,
        )
        rows, issues = providers.scan_non_claude(frozenset())
        assert walks == [frozenset({"codex", "kimi"})]
        assert {row.provider for row in rows} == {"codex", "kimi"}
        assert issues == ()


class TestResolveArgvExecution:
    def test_missing_sid_refused(self, monkeypatch, codex_home):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(),
        )
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert not resolution.success
        assert "missing session id" in resolution.detail

    def test_current_session_refused(self, monkeypatch, codex_home, tmp_path):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        cwd = tmp_path / "proj"
        cwd.mkdir()
        _write_rollout(
            codex_home,
            "01",
            f"rollout-a-{UUID1}.jsonl",
            {
                "id": UUID1,
                "session_id": UUID1,
                "cwd": str(cwd),
                "thread_source": "user",
            },
        )
        import os

        my_ancestor = os.getpid()
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(
                records=(_proc(my_ancestor, "codex", "resume", UUID1),),
            ),
        )
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert not resolution.success
        assert "current session" in resolution.detail

    def test_happy_path_returns_fresh_whole_session_with_residency(
        self,
        monkeypatch,
        codex_home,
        tmp_path,
    ):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        cwd = tmp_path / "proj"
        cwd.mkdir()
        _write_rollout(
            codex_home,
            "01",
            f"rollout-a-{UUID1}.jsonl",
            {
                "id": UUID1,
                "session_id": UUID1,
                "cwd": str(cwd),
                "thread_source": "user",
            },
        )
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(
                records=(_proc(999999, "codex", "resume", UUID1, starttime="88"),),
            ),
        )
        from cc_session_control.data.tmux_outcomes import ResidencyInventory

        seen_pids: list[set[int]] = []

        def fake_residency(pids):
            seen_pids.append(set(pids))
            return ResidencyInventory(targets={999999: "proj:2"})

        monkeypatch.setattr(providers.tmux, "residency_inventory", fake_residency)
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert resolution.success
        assert resolution.session is not None
        assert resolution.session.pid == 999999
        assert resolution.session.proc_start == "88"
        # Residency is gathered fresh — the spawn-path guard and in-place
        # attach must see real evidence, not the model defaults.
        assert seen_pids == [{999999}]
        assert resolution.session.tmux_target == "proj:2"
        assert resolution.session.tmux_inventory_complete


class TestSourceDegradationSurfaces:
    def test_kimi_malformed_state_json_is_an_issue(self, kimi_home):
        sid = f"session_{UUID1}"
        session_dir = _write_kimi_session(kimi_home, sid, None)
        import os

        with open(os.path.join(session_dir, "state.json"), "w") as fh:
            fh.write("{not json")
        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())
        assert not scan.complete
        assert any("state.json" in (i.path or "") for i in scan.issues)
        (row,) = scan.sessions  # the row still lists, degraded to index fields
        assert row.label == "(untitled)"

    def test_codex_unparseable_first_line_is_one_aggregated_issue(
        self,
        codex_home,
    ):
        day = codex_home / "sessions" / "2026" / "08" / "01"
        day.mkdir(parents=True, exist_ok=True)
        (day / "rollout-bad.jsonl").write_text("garbage, not session_meta\n")
        (day / "rollout-empty.jsonl").write_text("")  # benign lazy-created
        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
        assert not scan.complete
        (issue,) = scan.issues
        assert "1 rollout file(s)" in issue.detail

    def test_fresh_install_scans_clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "codex_home", tmp_path / "codex-fresh")
        monkeypatch.setattr(cfg, "kimi_home", tmp_path / "kimi-fresh")
        (tmp_path / "codex-fresh").mkdir()
        (tmp_path / "kimi-fresh").mkdir()
        assert CodexProvider().available()
        assert KimiProvider().available()
        codex_scan = CodexProvider().discover(ProcCliInventory(), frozenset())
        kimi_scan = KimiProvider().discover(ProcCliInventory(), frozenset())
        assert codex_scan.sessions == () and codex_scan.complete
        assert kimi_scan.sessions == () and kimi_scan.complete


class TestScanCliArgvInventory:
    """Direct unit tests of the /proc argv walk against a fake /proc tree."""

    @staticmethod
    def _fake_proc(tmp_path, pid: int, cmdline: bytes, starttime: str = "777"):
        pid_dir = tmp_path / str(pid)
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "cmdline").write_bytes(cmdline)
        tail = ["S", "1"] + ["0"] * 17 + [starttime, "0", "0"]
        (pid_dir / "stat").write_text(f"{pid} (comm) " + " ".join(tail))

    def test_matches_basename_and_captures_starttime(self, tmp_path, monkeypatch):
        from cc_session_control.data import proc as proc_mod

        self._fake_proc(tmp_path, 42, b"codex\x00resume\x00abc\x00", "888")
        self._fake_proc(tmp_path, 43, b"python3\x00app.py\x00")
        (tmp_path / "not-a-pid").mkdir()
        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path))
        inventory = proc_mod.scan_cli_argv_inventory(frozenset({"codex"}))
        assert inventory.complete
        (record,) = inventory.records
        assert record.pid == 42
        assert record.argv == ("codex", "resume", "abc")
        assert record.starttime == "888"

    def test_missing_proc_degrades_to_issue(self, tmp_path, monkeypatch):
        from cc_session_control.data import proc as proc_mod

        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path / "absent"))
        inventory = proc_mod.scan_cli_argv_inventory(frozenset({"codex"}))
        assert not inventory.complete
        assert inventory.records == ()

    def test_malformed_stat_is_an_issue(self, tmp_path, monkeypatch):
        from cc_session_control.data import proc as proc_mod

        pid_dir = tmp_path / "42"
        pid_dir.mkdir()
        (pid_dir / "cmdline").write_bytes(b"codex\x00resume\x00abc\x00")
        (pid_dir / "stat").write_text("garbage")
        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path))
        inventory = proc_mod.scan_cli_argv_inventory(frozenset({"codex"}))
        assert not inventory.complete
        assert inventory.records == ()

    def test_disappeared_pid_is_skipped_silently(self, tmp_path, monkeypatch):
        from cc_session_control.data import proc as proc_mod

        (tmp_path / "42").mkdir()  # dir exists but no cmdline — race window
        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path))
        inventory = proc_mod.scan_cli_argv_inventory(frozenset({"codex"}))
        assert inventory.complete
        assert inventory.records == ()

    def test_unreadable_cmdline_is_an_issue(self, tmp_path, monkeypatch):
        import os

        from cc_session_control.data import proc as proc_mod

        self._fake_proc(tmp_path, 42, b"codex\x00resume\x00abc\x00")
        os.chmod(tmp_path / "42" / "cmdline", 0)
        monkeypatch.setattr(proc_mod, "_PROC", str(tmp_path))
        try:
            inventory = proc_mod.scan_cli_argv_inventory(frozenset({"codex"}))
        finally:
            os.chmod(tmp_path / "42" / "cmdline", 0o644)
        assert not inventory.complete


class TestResolveArgvExecutionBranches:
    def test_non_discovery_provider_refused(self, monkeypatch):
        monkeypatch.setattr(cfg, "providers", ("claude",))
        resolution = providers.resolve_argv_execution("claude", UUID1)
        assert not resolution.success
        assert "no execution-time discovery" in resolution.detail

    def test_incomplete_ancestors_fail_closed(self, monkeypatch, codex_home):
        from cc_session_control.data.proc import AncestorProbe, ProcIssue

        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        monkeypatch.setattr(
            providers.proc,
            "probe_current_ancestors",
            lambda: AncestorProbe(frozenset(), (ProcIssue("x", None, "boom"),)),
        )
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert not resolution.success
        assert "ancestor evidence incomplete" in resolution.detail

    def test_incomplete_walk_fails_closed(self, monkeypatch, codex_home):
        from cc_session_control.data.proc import ProcIssue

        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(
                issues=(ProcIssue("CLI process inventory", None, "no /proc"),),
            ),
        )
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert not resolution.success
        assert "process evidence incomplete" in resolution.detail

    def test_incomplete_discovery_fails_closed(self, monkeypatch, codex_home):
        from cc_session_control.data.providers.base import ProviderScan
        from cc_session_control.data.providers.codex import CodexProvider
        from cc_session_control.models import InventoryIssue

        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(),
        )
        monkeypatch.setattr(
            CodexProvider,
            "discover",
            lambda self, inv, cur: ProviderScan(
                issues=(InventoryIssue("codex sessions", None, "broken"),),
            ),
        )
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert not resolution.success
        assert "session discovery incomplete" in resolution.detail

    def test_dead_session_resolves_without_residency_probe(
        self,
        monkeypatch,
        codex_home,
        tmp_path,
    ):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        cwd = tmp_path / "proj"
        cwd.mkdir()
        _write_rollout(
            codex_home,
            "01",
            f"rollout-a-{UUID1}.jsonl",
            {
                "id": UUID1,
                "session_id": UUID1,
                "cwd": str(cwd),
                "thread_source": "user",
            },
        )
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(),
        )
        monkeypatch.setattr(
            providers.tmux,
            "residency_inventory",
            lambda pids: (_ for _ in ()).throw(AssertionError("no probe")),
        )
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert resolution.success
        assert resolution.session is not None
        assert not resolution.session.alive


class TestCodexIndexDegradation:
    def test_unreadable_index_is_an_issue(self, codex_home):
        import os

        _write_rollout(
            codex_home,
            "01",
            f"rollout-a-{UUID1}.jsonl",
            {
                "id": UUID1,
                "session_id": UUID1,
                "cwd": "/tmp",
                "thread_source": "user",
            },
        )
        index = codex_home / "session_index.jsonl"
        index.write_text("{}")
        os.chmod(index, 0)
        try:
            scan = CodexProvider().discover(ProcCliInventory(), frozenset())
        finally:
            os.chmod(index, 0o644)
        assert not scan.complete
        (row,) = scan.sessions  # rows still list with degraded labels
        assert row.label == "(untitled)"
