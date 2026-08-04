"""Codex/kimi disk discovery + argv-exact liveness (ADR-0005)."""

from __future__ import annotations

import json

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.providers import codex as codex_mod
from cc_session_control.data.providers import kimi as kimi_mod
from cc_session_control.data.providers.codex import CodexProvider, extract_sid
from cc_session_control.data.providers.kimi import KimiProvider
from cc_session_control.data.proc import ProcCli, ProcCliInventory

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"
UUID2 = "019fc790-3126-7601-9e9b-fd378524ada8"


def _proc(pid: int, *argv: str, starttime: str = "100") -> ProcCli:
    return ProcCli(pid=pid, argv=tuple(argv), starttime=starttime)


class TestCodexExtract:
    def test_resume_and_fork_match(self):
        assert extract_sid(("codex", "resume", UUID1)) == UUID1
        assert extract_sid(("/usr/bin/codex", "fork", UUID1)) == UUID1

    def test_bare_and_daemon_argvs_never_match(self):
        assert extract_sid(("codex",)) is None
        assert extract_sid(("codex", "resume")) is None
        assert extract_sid(("codex", "app-server", "proxy")) is None
        assert (
            extract_sid(("codex", "-c", "features.x=true", "app-server")) is None
        )
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
        old = codex_home / "sessions" / "2026" / "08" / "01" / f"rollout-old-{UUID1}.jsonl"
        new = codex_home / "sessions" / "2026" / "08" / "02" / f"rollout-new-{UUID1}.jsonl"
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

    def test_happy_path_returns_fresh_whole_session(
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
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert resolution.success
        assert resolution.session is not None
        assert resolution.session.pid == 999999
        assert resolution.session.proc_start == "88"
