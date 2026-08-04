"""Codex resume argv binding (candidate B5): exec misbind tightened +
name-resume bound via `session_index.jsonl`.

`codex resume --help` (0.146.0): `codex resume [OPTIONS] [SESSION_ID]
[PROMPT]` — the positional target is "Session id (UUID) or session name.
UUIDs take precedence if it parses". Binding is the SIGTERM aiming rule for
`s`/takeover, so every uncertain argv shape must stay UNBOUND (fail closed:
a blind spot only risks a visible resume collision; a misbind kills the
wrong process). Own file — test_providers_discovery.py is over the 600-line
soft budget; the former `TestCodexExtract` cases live on here under the new
grammar/extractor seams.
"""

from __future__ import annotations

import json

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.proc import ProcCli, ProcCliInventory
from cc_session_control.data.providers.codex import (
    CodexProvider,
    extract_resume_target,
    sid_extractor,
)
from cc_session_control.data.tmux_outcomes import ResidencyInventory

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"
UUID2 = "019fc790-3126-7601-9e9b-fd378524ada8"


class TestResumeTargetGrammar:
    """Pure grammar seam: which token, if any, is the resume target."""

    def test_target_is_the_token_right_after_resume(self):
        assert extract_resume_target(("codex", "resume", UUID1)) == UUID1
        assert extract_resume_target(("/usr/bin/codex", "resume", "my-thread")) == (
            "my-thread"
        )
        # `codex resume <target> [PROMPT]` — prompt tokens are never targets.
        assert extract_resume_target(("codex", "resume", UUID1, "do", "x")) == UUID1

    def test_exec_before_resume_is_prompt_text_not_a_subcommand(self):
        assert (
            extract_resume_target(("codex", "exec", "fix", "resume", "bug", UUID1))
            is None
        )
        assert extract_resume_target(("codex", "exec", "resume", UUID1)) is None

    def test_pre_resume_global_flags_are_allowed(self):
        assert extract_resume_target(("codex", "--cd", "/x", "resume", UUID1)) == UUID1

    def test_missing_or_flag_target_stays_unbound(self):
        assert extract_resume_target(("codex", "resume")) is None
        assert extract_resume_target(("codex", "resume", "--last")) is None
        # Options take values (`-c k=v`): flags are never skipped to hunt
        # for a target, or a flag VALUE could bind as a session name.
        assert extract_resume_target(("codex", "resume", "-c", "k=v", UUID1)) is None

    def test_tokens_before_resume_are_never_the_target(self):
        assert extract_resume_target(("codex", UUID1, "resume")) is None

    def test_fork_bare_daemon_and_foreign_argvs_never_match(self):
        assert extract_resume_target(("codex", "fork", UUID1)) is None
        assert extract_resume_target(("codex",)) is None
        assert extract_resume_target(("codex", "app-server", "proxy")) is None
        assert (
            extract_resume_target(("codex", "-c", "features.x=true", "app-server"))
            is None
        )
        assert extract_resume_target(("kimi", "resume", UUID1)) is None


class TestSidExtractor:
    """Binding seam: raw target → sid, via UUID or the unique-name mapping."""

    def test_uuid_binds_lowercased_without_any_index(self):
        assert sid_extractor({})(("codex", "resume", UUID1.upper())) == UUID1

    def test_name_binds_only_through_the_mapping(self):
        extract = sid_extractor({"my-thread": UUID1})
        assert extract(("codex", "resume", "my-thread")) == UUID1
        assert extract(("codex", "resume", "ghost")) is None

    def test_name_without_mapping_stays_unbound(self):
        assert sid_extractor({})(("codex", "resume", "not-a-uuid")) is None

    def test_exec_prompt_uuid_never_binds(self):
        extract = sid_extractor({})
        assert extract(("codex", "exec", "fix", "resume", "bug", UUID1)) is None


def _proc(pid: int, *argv: str, starttime: str = "100") -> ProcCli:
    return ProcCli(pid=pid, argv=tuple(argv), starttime=starttime)


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


def _write_rollout(root, name: str, sid: str, cwd: str = "/tmp/proj") -> None:
    directory = root / "sessions" / "2026" / "08" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": sid,
        "session_id": sid,
        "cwd": cwd,
        "originator": "codex_cli_rs",
        "thread_source": "user",
    }
    record = {"timestamp": "t", "type": "session_meta", "payload": payload}
    (directory / name).write_text(json.dumps(record) + "\n")


def _write_index(root, *entries: dict) -> None:
    lines = [json.dumps(entry) for entry in entries]
    (root / "session_index.jsonl").write_text("\n".join(lines) + "\n")


def _discover(*records: ProcCli):
    inventory = ProcCliInventory(records=tuple(records))
    return CodexProvider().discover(inventory, cur=frozenset())


class TestDiscoverArgvBinding:
    """Generation-scan path: scan_non_claude → argv join → discover."""

    def test_uuid_inside_exec_prompt_never_binds(self, codex_home):
        # `codex exec fix resume bug <uuid>` is a headless exec run whose
        # unquoted PROMPT happens to contain "resume" and a uuid; binding it
        # would make the exec process the SIGTERM target for that sid.
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        scan = _discover(_proc(42, "codex", "exec", "fix", "resume", "bug", UUID1))
        (row,) = scan.sessions
        assert not row.alive
        assert row.pid is None

    def test_uuid_before_resume_never_binds(self, codex_home):
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        scan = _discover(_proc(42, "codex", UUID1, "resume"))
        (row,) = scan.sessions
        assert not row.alive

    def test_pre_resume_global_flags_still_bind(self, codex_home):
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        scan = _discover(
            _proc(42, "codex", "--cd", "/x", "resume", UUID1, starttime="777"),
        )
        (row,) = scan.sessions
        assert row.alive and row.pid == 42 and row.proc_start == "777"

    def test_plain_resume_still_binds(self, codex_home):
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        scan = _discover(_proc(42, "codex", "resume", UUID1))
        (row,) = scan.sessions
        assert row.alive and row.pid == 42

    def test_picker_and_last_stay_unbound(self, codex_home):
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        scan = _discover(
            _proc(42, "codex", "resume", "--last"),
            _proc(43, "codex", "resume"),
        )
        (row,) = scan.sessions
        assert not row.alive

    def test_name_resume_binds_via_unique_index(self, codex_home):
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        _write_index(codex_home, {"id": UUID1, "thread_name": "my-thread"})
        scan = _discover(_proc(42, "codex", "resume", "my-thread", starttime="777"))
        (row,) = scan.sessions
        assert row.alive and row.pid == 42 and row.proc_start == "777"

    def test_duplicate_thread_name_binds_nothing(self, codex_home):
        # One name owned by two sids: a guess would aim SIGTERM at one of
        # two processes — keep the blind spot instead.
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        _write_rollout(codex_home, f"rollout-b-{UUID2}.jsonl", UUID2)
        _write_index(
            codex_home,
            {"id": UUID1, "thread_name": "my-thread"},
            {"id": UUID2, "thread_name": "my-thread"},
        )
        scan = _discover(_proc(42, "codex", "resume", "my-thread"))
        assert all(not row.alive for row in scan.sessions)

    def test_unknown_name_binds_nothing(self, codex_home):
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        scan = _discover(_proc(42, "codex", "resume", "ghost"))
        (row,) = scan.sessions
        assert not row.alive

    def test_renamed_thread_binds_new_name_not_old(self, codex_home):
        # Last write wins in the append-only index: after a rename the old
        # name is stale evidence and must stop binding.
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1)
        _write_index(
            codex_home,
            {"id": UUID1, "thread_name": "old-name"},
            {"id": UUID1, "thread_name": "new-name"},
        )
        (row,) = _discover(_proc(42, "codex", "resume", "old-name")).sessions
        assert not row.alive
        (row,) = _discover(_proc(42, "codex", "resume", "new-name")).sessions
        assert row.alive and row.pid == 42


class TestNameResumeExecutionTakeover:
    """Execution-time path: resolve_argv_execution re-scans through the same
    discover, so a name-resumed process must re-resolve for live takeover."""

    def test_resolves_fresh_session_for_name_resume_process(
        self,
        codex_home,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        cwd = tmp_path / "proj"
        cwd.mkdir()
        _write_rollout(codex_home, f"rollout-a-{UUID1}.jsonl", UUID1, cwd=str(cwd))
        _write_index(codex_home, {"id": UUID1, "thread_name": "my-thread"})
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda basenames: ProcCliInventory(
                records=(
                    _proc(999999, "codex", "resume", "my-thread", starttime="88"),
                ),
            ),
        )
        monkeypatch.setattr(
            providers.tmux,
            "residency_inventory",
            lambda pids: ResidencyInventory(targets={}),
        )
        resolution = providers.resolve_argv_execution("codex", UUID1)
        assert resolution.success
        assert resolution.session is not None
        assert resolution.session.alive
        assert resolution.session.pid == 999999
        assert resolution.session.proc_start == "88"
