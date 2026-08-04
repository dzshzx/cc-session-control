"""Codex `Session.source` classification (candidate B9a).

`session_meta` carries richer launch-surface evidence than the old two-bucket
`codex_exec -> sdk / else -> cli` mapping used: real rollouts on this machine
show `"originator":"Codex Desktop","source":"vscode"` and
`"originator":"codex_chatgpt_android_remote"`, both previously flattened into
the misleading "CLI" badge. Kept in its own file (rather than growing
test_providers_discovery.py, already near the 600-line soft budget) so this
focused mapping stays independently reviewable/removable — mirrors the split
already used for test_providers_labels.py.
"""

from __future__ import annotations

import json

import pytest

from cc_session_control.config import cfg
from cc_session_control.data.proc import ProcCliInventory
from cc_session_control.data.providers.codex import CodexProvider, _classify_source

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


def _meta_payload(sid: str, **overrides) -> dict:
    payload = {
        "id": sid,
        "session_id": sid,
        "cwd": "/tmp/proj",
        "originator": "codex_cli_rs",
        "thread_source": "user",
    }
    payload.update(overrides)
    return payload


def _write_rollout(root, name: str, sid: str, **overrides) -> None:
    directory = root / "sessions" / "2026" / "08" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    meta_line = {
        "timestamp": "t",
        "type": "session_meta",
        "payload": _meta_payload(sid, **overrides),
    }
    (directory / name).write_text(json.dumps(meta_line) + "\n")


def _discover_row(codex_home):
    scan = CodexProvider().discover(ProcCliInventory(), cur=frozenset())
    assert scan.complete
    (row,) = scan.sessions
    return row


class TestClassifySourcePure:
    """Unit-level coverage of the pure priority ladder, no disk IO."""

    def test_codex_exec_is_sdk(self):
        payload = _meta_payload(UUID1, originator="codex_exec")
        assert _classify_source(payload) == "sdk"

    def test_vscode_source_field_is_vscode(self):
        payload = _meta_payload(UUID1, originator="Codex Desktop", source="vscode")
        assert _classify_source(payload) == "vscode"

    def test_exact_android_remote_originator_is_remote(self):
        payload = _meta_payload(UUID1, originator="codex_chatgpt_android_remote")
        assert _classify_source(payload) == "remote"

    def test_similar_but_inexact_originator_stays_cli(self):
        # Exact-match discipline (same rule as sid_extractor's name binding):
        # a near-miss must not be guessed into the "remote" badge.
        payload = _meta_payload(UUID1, originator="codex_chatgpt_android_remote_beta")
        assert _classify_source(payload) == "cli"

    def test_codex_exec_wins_over_vscode_and_remote(self):
        # codex_exec is the pre-existing bridge/SDK hide signal and must win
        # even if source/originator also carry vscode/remote evidence.
        payload = _meta_payload(
            UUID1,
            originator="codex_exec",
            source="vscode",
        )
        assert _classify_source(payload) == "sdk"

    def test_plain_cli_originator_stays_cli(self):
        payload = _meta_payload(UUID1, originator="codex_cli_rs")
        assert _classify_source(payload) == "cli"


class TestCodexDiscoverSourceBadges:
    """End-to-end through `discover()`, mirroring the real rollout shapes."""

    def test_vscode_desktop_rollout_gets_vscode_source(self, codex_home):
        _write_rollout(
            codex_home,
            f"rollout-a-{UUID1}.jsonl",
            UUID1,
            originator="Codex Desktop",
            source="vscode",
        )
        row = _discover_row(codex_home)
        assert row.source == "vscode"
        assert not row.bridge_or_sdk  # must stay visible under the `h` toggle

    def test_android_remote_rollout_gets_remote_source(self, codex_home):
        _write_rollout(
            codex_home,
            f"rollout-b-{UUID1}.jsonl",
            UUID1,
            originator="codex_chatgpt_android_remote",
        )
        row = _discover_row(codex_home)
        assert row.source == "remote"
        assert not row.bridge_or_sdk  # must stay visible under the `h` toggle

    def test_codex_exec_rollout_still_sdk_and_hidden(self, codex_home):
        _write_rollout(
            codex_home,
            f"rollout-c-{UUID1}.jsonl",
            UUID1,
            originator="codex_exec",
        )
        row = _discover_row(codex_home)
        assert row.source == "sdk"
        assert row.bridge_or_sdk

    def test_plain_cli_rollout_unchanged(self, codex_home):
        _write_rollout(codex_home, f"rollout-d-{UUID1}.jsonl", UUID1)
        row = _discover_row(codex_home)
        assert row.source == "cli"
