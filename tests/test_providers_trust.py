"""Tests for the provider trust-store readers (ADR-0007).

codex `config.toml [projects.*]` and kimi `workspace-trust/<id>` records are
membership evidence only: exact-match directories, missing stores are not
issues, and unreadable/malformed stores narrow only their own source.
"""

from __future__ import annotations

import json

from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.providers import codex_trust, kimi


def _codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


def _kimi_home(tmp_path, monkeypatch):
    home = tmp_path / "kimi"
    home.mkdir()
    monkeypatch.setattr(cfg, "kimi_home", home)
    return home


# --- codex config.toml [projects.*] ------------------------------------------


def test_codex_missing_config_is_empty_without_issue(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)

    scan = codex_trust.read_trusted_dirs(home)

    assert scan.directories == ()
    assert scan.issues == ()


def test_codex_trusted_keys_only(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    (home / "config.toml").write_text(
        '[projects."/a"]\ntrust_level = "trusted"\n'
        '[projects."/b"]\ntrust_level = "untrusted"\n'
        '[projects."/c"]\n'
        "[other]\nkey = 1\n"
    )

    scan = codex_trust.read_trusted_dirs(home)

    assert scan.directories == ("/a",)
    assert scan.issues == ()


def test_codex_malformed_toml_narrows_only_this_source(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    (home / "config.toml").write_text("[projects\n")

    scan = codex_trust.read_trusted_dirs(home)

    assert scan.directories == ()
    assert [issue.source for issue in scan.issues] == ["codex trust"]


def test_codex_non_table_projects_is_an_issue(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, monkeypatch)
    (home / "config.toml").write_text("projects = 5\n")

    scan = codex_trust.read_trusted_dirs(home)

    assert scan.directories == ()
    assert [issue.source for issue in scan.issues] == ["codex trust"]


# --- kimi workspace-trust/<id> ------------------------------------------------


def test_kimi_missing_trust_dir_is_empty_without_issue(tmp_path, monkeypatch):
    _kimi_home(tmp_path, monkeypatch)

    scan = kimi._read_trusted_dirs()

    assert scan.directories == ()
    assert scan.issues == ()


def test_kimi_trust_records_yield_roots(tmp_path, monkeypatch):
    home = _kimi_home(tmp_path, monkeypatch)
    trust = home / "workspace-trust"
    trust.mkdir()
    (trust / "wd_a_1").write_text(json.dumps({"root": "/a", "trustedAt": 1}))
    (trust / "wd_b_2").write_text(
        json.dumps({"root": "/b", "trustedAt": 1720000000000})
    )

    scan = kimi._read_trusted_dirs()

    assert scan.directories == ("/a", "/b")
    assert scan.issues == ()


def test_kimi_malformed_records_are_skipped_with_issues(tmp_path, monkeypatch):
    home = _kimi_home(tmp_path, monkeypatch)
    trust = home / "workspace-trust"
    trust.mkdir()
    (trust / "wd_ok_1").write_text(json.dumps({"root": "/a", "trustedAt": 1}))
    (trust / "wd_broken_2").write_text("{not json")
    (trust / "wd_noroot_3").write_text(json.dumps({"trustedAt": 1}))
    (trust / "wd_relative_4").write_text(json.dumps({"root": "rel", "trustedAt": 1}))

    scan = kimi._read_trusted_dirs()

    assert scan.directories == ("/a",)
    assert len(scan.issues) == 3
    assert {issue.source for issue in scan.issues} == {"kimi trust"}


# --- registry aggregation ------------------------------------------------------


def test_scan_trusted_dirs_covers_active_providers_only(tmp_path, monkeypatch):
    codex_home = _codex_home(tmp_path, monkeypatch)
    (codex_home / "config.toml").write_text(
        '[projects."/a"]\ntrust_level = "trusted"\n'
    )
    kimi_home = _kimi_home(tmp_path, monkeypatch)
    trust = kimi_home / "workspace-trust"
    trust.mkdir()
    (trust / "wd_a_1").write_text(json.dumps({"root": "/b", "trustedAt": 1}))
    monkeypatch.setattr(cfg, "providers", ("claude", "codex", "kimi"))

    scan = providers.scan_trusted_dirs()

    # Claude never appears here — its trust store is ~/.claude.json.
    assert scan.directories == {"codex": ("/a",), "kimi": ("/b",)}
    assert scan.issues == ()

    monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
    scan = providers.scan_trusted_dirs()
    assert scan.directories == {"codex": ("/a",)}
