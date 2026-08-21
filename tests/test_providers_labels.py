"""Codex label fallback: rollout-body first user message (candidate A3).

`session_index.jsonl` thread_name barely overlaps active rollouts on real
machines, so discovery falls back to a bounded continuation read of the
rollout body for the first real `user_message` event. Kept in its own file
(rather than growing the already broad test_providers_discovery.py) so this
focused fallback behavior stays independently reviewable/removable.
"""

from __future__ import annotations

import json

import pytest

from cc_session_control.config import cfg
from cc_session_control.data.proc import ProcCliInventory
from cc_session_control.data.providers import codex as codex_mod
from cc_session_control.data.providers import codex_rollout
from cc_session_control.data.providers.codex import CodexProvider

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


def _meta_payload(sid: str) -> dict:
    return {
        "id": sid,
        "session_id": sid,
        "cwd": "/tmp/proj",
        "originator": "codex_cli_rs",
        "thread_source": "user",
    }


def _user_message_event(message: str) -> dict:
    return {
        "timestamp": "t",
        "type": "event_msg",
        "payload": {"type": "user_message", "message": message},
    }


def _write_rollout(root, name: str, sid: str, body_lines: list[dict]) -> None:
    directory = root / "sessions" / "2026" / "08" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    meta_line = {
        "timestamp": "t",
        "type": "session_meta",
        "payload": _meta_payload(sid),
    }
    lines = [json.dumps(meta_line)] + [json.dumps(line) for line in body_lines]
    (directory / name).write_text("\n".join(lines) + "\n")


def _discover_label(codex_home) -> str:
    scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
    assert scan.complete
    (row,) = scan.sessions
    return row.label


class TestCodexBodyLabelFallback:
    def test_no_index_name_falls_back_to_first_user_message(self, codex_home):
        _write_rollout(
            codex_home,
            f"rollout-a-{UUID1}.jsonl",
            UUID1,
            [_user_message_event("  hello   \n there  ")],
        )
        assert _discover_label(codex_home) == "hello there"

    def test_index_name_wins_over_body_fallback(self, codex_home):
        _write_rollout(
            codex_home,
            f"rollout-b-{UUID1}.jsonl",
            UUID1,
            [_user_message_event("real question here")],
        )
        (codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": UUID1, "thread_name": "调研任务"}) + "\n"
        )
        assert _discover_label(codex_home) == "调研任务"

    def test_message_beyond_line_cap_is_untitled(self, codex_home, monkeypatch):
        monkeypatch.setattr(codex_rollout, "_BODY_SCAN_MAX_LINES", 2)
        _write_rollout(
            codex_home,
            f"rollout-c-{UUID1}.jsonl",
            UUID1,
            [
                # Two non-user_message filler lines exhaust the 2-line cap
                # before the real message at line 3 is ever reached.
                {"timestamp": "t", "type": "event_msg", "payload": {"type": "other"}},
                {"timestamp": "t", "type": "event_msg", "payload": {"type": "other"}},
                _user_message_event("too far to see"),
            ],
        )
        assert _discover_label(codex_home) == "(untitled)"

    def test_wrapper_block_is_skipped_for_next_real_message(self, codex_home):
        _write_rollout(
            codex_home,
            f"rollout-e-{UUID1}.jsonl",
            UUID1,
            [
                _user_message_event("<user_instructions>be nice</user_instructions>"),
                _user_message_event("real question here"),
            ],
        )
        assert _discover_label(codex_home) == "real question here"

    def test_no_user_message_at_all_is_untitled(self, codex_home):
        _write_rollout(
            codex_home,
            f"rollout-f-{UUID1}.jsonl",
            UUID1,
            [
                {
                    "timestamp": "t",
                    "type": "event_msg",
                    "payload": {"type": "agent_message"},
                }
            ],
        )
        assert _discover_label(codex_home) == "(untitled)"

    def test_first_line_over_old_cap_under_new_cap_is_discovered(self, codex_home):
        # 100KB session_meta line: exceeds the old 64KB read cap but fits
        # under the 256KB one — real machines have observed lines near 35KB,
        # so this exercises the headroom the cap raise buys.
        meta = _meta_payload(UUID1)
        meta["pad"] = "x" * 100_000
        directory = codex_home / "sessions" / "2026" / "08" / "01"
        directory.mkdir(parents=True, exist_ok=True)
        meta_line = json.dumps(
            {"timestamp": "t", "type": "session_meta", "payload": meta}
        )
        assert 64 * 1024 < len(meta_line) < codex_mod.FIRST_LINE_CAP
        (directory / f"rollout-h-{UUID1}.jsonl").write_text(meta_line + "\n")

        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())

        assert scan.complete
        (row,) = scan.sessions
        assert row.sid == UUID1

    def test_first_line_over_new_cap_reports_a_cap_issue(self, codex_home):
        meta = _meta_payload(UUID1)
        meta["pad"] = "x" * 300_000
        directory = codex_home / "sessions" / "2026" / "08" / "01"
        directory.mkdir(parents=True, exist_ok=True)
        meta_line = json.dumps(
            {"timestamp": "t", "type": "session_meta", "payload": meta}
        )
        assert len(meta_line) > codex_mod.FIRST_LINE_CAP
        (directory / f"rollout-i-{UUID1}.jsonl").write_text(meta_line + "\n")

        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())

        assert scan.sessions == ()
        assert not scan.complete
        (issue,) = scan.issues
        assert str(codex_mod.FIRST_LINE_CAP) in issue.detail
        assert "cap" in issue.detail
        assert "upstream format change" not in issue.detail

    def test_malformed_body_line_is_skipped_silently(self, codex_home):
        directory = codex_home / "sessions" / "2026" / "08" / "01"
        directory.mkdir(parents=True, exist_ok=True)
        meta_line = json.dumps(
            {"timestamp": "t", "type": "session_meta", "payload": _meta_payload(UUID1)}
        )
        # A torn/malformed line containing the marker must not raise or mark
        # the scan incomplete — it is skipped and the scan keeps going.
        broken = '{"type": "event_msg", "payload": {"type": "user_message"'
        real = json.dumps(_user_message_event("real question here"))
        (directory / f"rollout-g-{UUID1}.jsonl").write_text(
            "\n".join([meta_line, broken, real]) + "\n"
        )
        scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
        assert scan.complete
        (row,) = scan.sessions
        assert row.label == "real question here"
