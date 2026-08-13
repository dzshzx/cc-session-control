"""Tests for the evidence-tier membership computation (ADR-0007).

`compute_membership` is pure except for `os.path.isdir`, so every candidate
directory is a real dir under tmp_path (which itself lives under the real
temp root — the autouse fixture neutralizes the temp filter unless a test
injects roots explicitly). `NOW` is a realistic epoch so `mtime=0.0`
sessions read as decayed.
"""

from __future__ import annotations

import json

import pytest

from cc_session_control.data import membership
from cc_session_control.models import Session

NOW = 1_800_000_000.0
RECENT = NOW - 100.0
OLD = NOW - (membership.OBSERVED_DECAY_DAYS + 10) * 86400.0


@pytest.fixture(autouse=True)
def _no_temp_roots(monkeypatch):
    monkeypatch.setattr(membership, "_TEMP_ROOTS", frozenset())


def _session(cwd: str, mtime: float, provider: str = "claude") -> Session:
    return Session(
        sid=f"s-{provider}-{mtime}",
        cwd=cwd,
        label="",
        mtime=mtime,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        provider=provider,
    )


def _compute(tmp_path, **overrides):
    defaults = dict(
        claude_projects=None,
        provider_trust={},
        sessions=(),
        pinned=frozenset(),
        hidden=frozenset(),
        window_paths=(),
        now=NOW,
    )
    defaults.update(overrides)
    return membership.compute_membership(**defaults)


def _by_dir(entries):
    return {entry.directory: entry for entry in entries}


# --- trusted tier ------------------------------------------------------------


def test_claude_trusted_key_is_a_member(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    projects = {str(proj): {"hasTrustDialogAccepted": True}}

    rows = _by_dir(_compute(tmp_path, claude_projects=projects))

    assert str(proj) in rows
    assert rows[str(proj)].trusted_by == frozenset({"claude"})
    assert rows[str(proj)].observed_by == frozenset()


def test_claude_unavailable_contributes_nothing(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # None = unreadable claude.json evidence — no candidates, no badges.
    assert _compute(tmp_path, claude_projects=None) == []


def test_provider_trusted_dirs_are_members(tmp_path):
    codex_proj = tmp_path / "cx-proj"
    kimi_proj = tmp_path / "km-proj"
    codex_proj.mkdir()
    kimi_proj.mkdir()

    rows = _by_dir(
        _compute(
            tmp_path,
            provider_trust={
                "codex": (str(codex_proj),),
                "kimi": (str(kimi_proj), "relative/junk"),
            },
        )
    )

    assert rows[str(codex_proj)].trusted_by == frozenset({"codex"})
    assert rows[str(kimi_proj)].trusted_by == frozenset({"kimi"})
    assert all(row.directory.startswith("/") for row in rows.values())


def test_inheritance_qualifies_but_never_generates(tmp_path):
    # The load-bearing invariant: a trusted ancestor (even `/`) does NOT
    # enumerate its descendants — only recorded keys and observed cwds are
    # candidates. The observed child earns the claude badge through
    # inheritance (effective trust == the RC start gate).
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    projects = {str(parent): {"hasTrustDialogAccepted": True}}

    rows = _by_dir(
        _compute(
            tmp_path,
            claude_projects=projects,
            sessions=(_session(str(child), RECENT, "kimi"),),
        )
    )

    assert set(rows) == {str(parent), str(child)}
    assert rows[str(parent)].trusted_by == frozenset({"claude"})
    assert rows[str(child)].observed_by == frozenset({"kimi"})
    assert rows[str(child)].trusted_by == frozenset({"claude"})  # badge via inheritance


def test_trusted_root_does_not_flood(tmp_path):
    root_child = tmp_path / "somewhere"
    root_child.mkdir()
    projects = {"/": {"hasTrustDialogAccepted": True}}

    rows = _by_dir(
        _compute(
            tmp_path,
            claude_projects=projects,
            sessions=(_session(str(root_child), RECENT),),
        )
    )

    # Only the recorded key and the observed cwd — never the whole tree.
    assert set(rows) == {"/", str(root_child)}
    assert rows[str(root_child)].trusted_by == frozenset({"claude"})


# --- observed tier + decay ---------------------------------------------------


def test_observed_recent_activity_is_a_member(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()

    rows = _by_dir(
        _compute(
            tmp_path,
            sessions=(
                _session(str(proj), OLD, "codex"),
                _session(str(proj), RECENT, "kimi"),
            ),
        )
    )

    row = rows[str(proj)]
    assert row.observed_by == frozenset({"codex", "kimi"})
    assert row.last_activity == RECENT
    assert row.trusted_by == frozenset()


def test_observed_only_decays_after_inactivity(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()

    assert _compute(tmp_path, sessions=(_session(str(proj), OLD),)) == []


def test_zero_mtime_is_not_recency_evidence(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    # A stat-failed session (mtime 0.0) must not pin a directory on the tab.
    assert _compute(tmp_path, sessions=(_session(str(proj), 0.0),)) == []


def test_trusted_or_pinned_never_decay(tmp_path):
    trusted = tmp_path / "trusted"
    pinned = tmp_path / "pinned"
    trusted.mkdir()
    pinned.mkdir()

    rows = _by_dir(
        _compute(
            tmp_path,
            claude_projects={str(trusted): {"hasTrustDialogAccepted": True}},
            pinned=frozenset({str(pinned)}),
            sessions=(
                _session(str(trusted), OLD),
                _session(str(pinned), OLD),
            ),
        )
    )

    assert set(rows) == {str(trusted), str(pinned)}
    assert rows[str(pinned)].pinned is True


def test_empty_cwd_sessions_are_ignored(tmp_path):
    assert _compute(tmp_path, sessions=(_session("", RECENT),)) == []


# --- hygiene (temp / missing dirs) and the escapes ---------------------------


def test_temp_trusted_dropped_but_window_or_pin_saves_it(tmp_path, monkeypatch):
    troot = tmp_path / "t"
    temp_proj = troot / "scratch"
    temp_proj.mkdir(parents=True)
    monkeypatch.setattr(membership, "_TEMP_ROOTS", frozenset({str(troot)}))
    projects = {str(temp_proj): {"hasTrustDialogAccepted": True}}

    assert _compute(tmp_path, claude_projects=projects) == []

    rows = _compute(
        tmp_path,
        claude_projects=projects,
        window_paths=(str(temp_proj),),
    )
    assert [row.directory for row in rows] == [str(temp_proj)]

    rows = _compute(
        tmp_path,
        claude_projects=projects,
        pinned=frozenset({str(temp_proj)}),
    )
    assert [row.directory for row in rows] == [str(temp_proj)]


def test_temp_segment_boundary(tmp_path, monkeypatch):
    sibling = tmp_path / "t-ext"
    sibling.mkdir()
    monkeypatch.setattr(membership, "_TEMP_ROOTS", frozenset({str(tmp_path / "t")}))

    rows = _compute(
        tmp_path,
        claude_projects={str(sibling): {"hasTrustDialogAccepted": True}},
    )
    assert [row.directory for row in rows] == [str(sibling)]


def test_missing_dir_dropped_unless_pinned_or_windowed(tmp_path):
    gone = tmp_path / "gone"
    gone_trusted = str(gone)
    projects = {gone_trusted: {"hasTrustDialogAccepted": True}}

    assert _compute(tmp_path, claude_projects=projects) == []

    rows = _by_dir(_compute(tmp_path, claude_projects=projects, pinned={gone_trusted}))
    assert rows[gone_trusted].dir_exists is False
    assert rows[gone_trusted].pinned is True

    rows = _by_dir(
        _compute(tmp_path, claude_projects=projects, window_paths=(gone_trusted,))
    )
    assert rows[gone_trusted].dir_exists is False
    assert rows[gone_trusted].has_window is True


# --- curation: pin / hide ----------------------------------------------------


def test_hidden_suppresses_every_tier(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()

    rows = _compute(
        tmp_path,
        claude_projects={str(proj): {"hasTrustDialogAccepted": True}},
        sessions=(_session(str(proj), RECENT),),
        pinned=frozenset({str(proj)}),  # pin+hidden: hidden wins
        hidden=frozenset({str(proj)}),
    )

    [row] = rows
    assert row.hidden is True
    assert row.pinned is False
    assert row.trusted_by == frozenset({"claude"})
    assert row.observed_by == frozenset({"claude"})


def test_hidden_entry_survives_decay_and_hygiene_for_the_unhide_verb(tmp_path):
    gone = str(tmp_path / "gone")  # never existed — hidden with zero evidence

    rows = _compute(tmp_path, hidden=frozenset({gone}))

    [row] = rows
    assert row.directory == gone
    assert row.hidden is True
    assert row.dir_exists is False


def test_normpath_identity_merges_trailing_slash_records(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()

    rows = _by_dir(
        _compute(
            tmp_path,
            provider_trust={"codex": (str(proj) + "/",)},
            sessions=(_session(str(proj) + "/", RECENT, "codex"),),
        )
    )

    assert set(rows) == {str(proj)}
    assert rows[str(proj)].trusted_by == frozenset({"codex"})
    assert rows[str(proj)].observed_by == frozenset({"codex"})


def test_entries_are_sorted_by_directory(tmp_path):
    for name in ("b", "a", "c"):
        (tmp_path / name).mkdir()

    rows = _compute(
        tmp_path,
        pinned=frozenset(str(tmp_path / name) for name in ("b", "a", "c")),
    )

    assert [row.directory for row in rows] == sorted(
        str(tmp_path / name) for name in ("b", "a", "c")
    )


# --- scan integration (rc.scan_result consumes membership) -------------------


def _wire_rc_scan(tmp_path, monkeypatch, projects=None, providers=("claude",)):
    import json

    from cc_session_control.data import rc

    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"projects": projects or {}}))
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    monkeypatch.setattr(rc.cfg, "providers", providers)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(rc.cfg, "codex_home", tmp_path / "codex-home")
    monkeypatch.setattr(rc.cfg, "kimi_home", tmp_path / "kimi-home")
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: rc.tmux.WindowInventory(),
    )
    return rc


def test_scan_lists_codex_only_directory(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    proj = tmp_path / "cx-only"
    proj.mkdir()
    (codex_home / "config.toml").write_text(
        f'[projects."{proj}"]\ntrust_level = "trusted"\n'
    )
    rc = _wire_rc_scan(tmp_path, monkeypatch, providers=("claude", "codex"))

    rows = {p.directory: p for p in rc.scan_result().projects}

    assert set(rows) == {str(proj)}
    assert rows[str(proj)].trusted_by == frozenset({"codex"})
    # Claude never trusted it — the RC start gate stays honestly refused.
    assert rows[str(proj)].trust_decision.value == "untrusted"


def test_scan_lists_kimi_observed_directory(tmp_path, monkeypatch):
    proj = tmp_path / "km-only"
    proj.mkdir()
    rc = _wire_rc_scan(tmp_path, monkeypatch)

    rows = {
        p.directory: p
        for p in rc.scan_result(
            sessions=(_session(str(proj), RECENT, "kimi"),)
        ).projects
    }

    assert set(rows) == {str(proj)}
    assert rows[str(proj)].observed_by == frozenset({"kimi"})


def test_scan_pinned_directory_without_any_cli_evidence(tmp_path, monkeypatch):
    proj = tmp_path / "mine"
    proj.mkdir()
    rc = _wire_rc_scan(tmp_path, monkeypatch)
    xdg = tmp_path / "xdg" / "csctl"
    xdg.mkdir(parents=True)
    (xdg / "projects.json").write_text(
        json.dumps({"pinned": [str(proj)], "hidden": []})
    )

    rows = {p.directory: p for p in rc.scan_result().projects}

    assert set(rows) == {str(proj)}
    assert rows[str(proj)].pinned is True


def test_scan_surfaces_membership_source_issues(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("[projects\n")  # broken TOML
    rc = _wire_rc_scan(tmp_path, monkeypatch, providers=("claude", "codex"))

    result = rc.scan_result()

    assert result.projects == []
    assert [issue.source for issue in result.membership_issues] == ["codex trust"]
