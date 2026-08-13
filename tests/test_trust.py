"""Tests for effective trust and path-keyed project membership.

`models.effective_trust_decision` mirrors Claude Code's runtime trust-dialog
gate (verified against claude 2.1.218 on 2026-07-23): the dialog is
suppressed when the cwd or ANY ancestor holds a `hasTrustDialogAccepted:
true` entry, and a suppressed subdirectory gets an entry with an EXPLICIT
False flag — while declining the dialog writes no entry at all. So explicit
False must never veto, and ancestor matching must respect path-segment
boundaries.
"""

from __future__ import annotations

from cc_session_control.models import (
    TrustDecision,
    effective_trust_decision,
)

WS = "/home/u/workspace"


def test_own_true_flag_is_trusted():
    projects = {WS + "/proj": {"hasTrustDialogAccepted": True}}
    assert effective_trust_decision(WS + "/proj", projects) is TrustDecision.TRUSTED


def test_no_entry_no_ancestor_is_untrusted():
    projects = {WS + "/proj": {"hasTrustDialogAccepted": True}}
    assert effective_trust_decision("/srv/other", projects) is TrustDecision.UNTRUSTED


def test_missing_flag_without_trusted_ancestor_is_untrusted():
    projects = {WS + "/proj": {}}
    assert effective_trust_decision(WS + "/proj", projects) is TrustDecision.UNTRUSTED


def test_explicit_false_without_trusted_ancestor_is_untrusted():
    projects = {WS + "/proj": {"hasTrustDialogAccepted": False}}
    assert effective_trust_decision(WS + "/proj", projects) is TrustDecision.UNTRUSTED


def test_ancestor_true_inherits_to_subdir():
    # The claude-2.1.218 shape: parent accepted, subdir entry explicit False.
    projects = {
        WS: {"hasTrustDialogAccepted": True},
        WS + "/new-proj": {"hasTrustDialogAccepted": False},
    }
    assert effective_trust_decision(WS + "/new-proj", projects) is TrustDecision.TRUSTED


def test_ancestor_true_inherits_without_own_entry():
    projects = {WS: {"hasTrustDialogAccepted": True}}
    assert (
        effective_trust_decision(WS + "/brand-new", projects) is TrustDecision.TRUSTED
    )


def test_ancestor_matches_any_depth():
    projects = {"/home/u": {"hasTrustDialogAccepted": True}}
    assert (
        effective_trust_decision(WS + "/deep/nested/dir", projects)
        is TrustDecision.TRUSTED
    )


def test_prefix_collision_is_not_an_ancestor():
    # /a/workspace must NOT cover /a/workspace-external (segment boundary).
    projects = {WS: {"hasTrustDialogAccepted": True}}
    assert (
        effective_trust_decision(WS + "-external/proj", projects)
        is TrustDecision.UNTRUSTED
    )


def test_root_trusted_covers_everything():
    # rstrip guard: root="/" must not degenerate to a never-matching "//".
    projects = {"/": {"hasTrustDialogAccepted": True}}
    assert effective_trust_decision("/any/dir", projects) is TrustDecision.TRUSTED


def test_trailing_slash_keys_normalize():
    projects = {WS + "/": {"hasTrustDialogAccepted": True}}
    assert effective_trust_decision(WS + "/proj", projects) is TrustDecision.TRUSTED
    assert effective_trust_decision(WS, projects) is TrustDecision.TRUSTED


def test_false_ancestor_does_not_inherit():
    projects = {
        WS: {"hasTrustDialogAccepted": False},
        WS + "/proj": {"hasTrustDialogAccepted": False},
    }
    assert effective_trust_decision(WS + "/proj", projects) is TrustDecision.UNTRUSTED


def test_non_dict_entry_is_ignored():
    projects = {WS: None, WS + "/proj": "garbage"}
    assert effective_trust_decision(WS + "/proj", projects) is TrustDecision.UNTRUSTED


def test_empty_inputs():
    assert (
        effective_trust_decision("", {WS: {"hasTrustDialogAccepted": True}})
        is TrustDecision.UNTRUSTED
    )
    assert effective_trust_decision(WS, {}) is TrustDecision.UNTRUSTED


def test_effective_trust_decision_keeps_unavailable_distinct_and_fail_closed():
    assert (
        effective_trust_decision(
            WS + "/proj",
            {WS: {"hasTrustDialogAccepted": True}},
        )
        is TrustDecision.TRUSTED
    )
    assert effective_trust_decision(WS + "/proj", {}) is TrustDecision.UNTRUSTED
    assert effective_trust_decision(WS + "/proj", None) is TrustDecision.UNAVAILABLE


# --- scan-level membership (path-keyed, inheritance-aware) ------------------


def _wire_scan(tmp_path, monkeypatch, projects, temp_roots=()):
    import json
    import os

    from cc_session_control.data import membership

    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"projects": projects}))
    monkeypatch.setattr(membership.cfg, "claude_json", cj)
    # pytest tmp_path lives under the REAL platform temp root, so the temp-dir
    # membership filter is neutralized unless a test injects roots explicitly.
    monkeypatch.setattr(
        membership,
        "_TEMP_ROOTS",
        frozenset(os.path.normpath(p) for p in temp_roots),
    )
    return membership


def test_scan_includes_inherited_subdir(tmp_path, monkeypatch):
    # The original bug: parent trusted, real project created underneath —
    # the subdir entry carries the explicit-False footprint and must appear.
    parent = tmp_path / "parent"
    sub = parent / "new-proj"
    sub.mkdir(parents=True)
    membership = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(parent): {"hasTrustDialogAccepted": True},
            str(sub): {"hasTrustDialogAccepted": False},
        },
    )

    rows = {p.directory: p for p in membership.scan_projects(()).projects}
    assert str(sub) in rows and str(parent) in rows
    assert "claude" in rows[str(sub)].trusted_by
    assert rows[str(sub)].name == "new-proj"


def test_scan_excludes_untrusted_entry(tmp_path, monkeypatch):
    # Explicit False with NO trusted ancestor (the hapi shape) stays out.
    lone = tmp_path / "lone"
    lone.mkdir()
    membership = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(lone): {"hasTrustDialogAccepted": False},
        },
    )

    assert membership.scan_projects(()).projects == ()


def test_scan_keeps_unavailable_settings_observable(tmp_path, monkeypatch):
    from cc_session_control.data import membership

    project = tmp_path / "app"
    project.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{broken")
    monkeypatch.setattr(membership.cfg, "claude_json", claude_json)
    monkeypatch.setattr(membership, "_TEMP_ROOTS", frozenset())

    scan = membership.scan_projects(())

    # Membership fails closed (no trusted set to enumerate), and the broken
    # settings evidence stays observable as a typed membership issue.
    assert scan.projects == ()
    assert any(issue.source == "claude.json project settings" for issue in scan.issues)


# --- temp-dir membership filter (trust untouched, discovery only) -----------


def test_is_temp_path_segment_boundary(monkeypatch):
    from cc_session_control.data import membership

    monkeypatch.setattr(membership, "_TEMP_ROOTS", frozenset({"/tmp"}))
    assert membership._is_temp_path("/tmp") is True
    assert membership._is_temp_path("/tmp/x/y") is True
    assert membership._is_temp_path("/tmpfoo") is False
    assert membership._is_temp_path("/home/u/tmp") is False


def test_scan_drops_temp_root_and_subtree(tmp_path, monkeypatch):
    # The motivating shape: /tmp itself trusted (kept trusted on purpose so
    # scratchpad sessions skip the dialog) must not surface as a project.
    troot = tmp_path / "t"
    sub = troot / "scratch"
    sub.mkdir(parents=True)
    membership = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(troot): {"hasTrustDialogAccepted": True},
            str(sub): {"hasTrustDialogAccepted": False},
        },
        temp_roots={str(troot)},
    )

    assert membership.scan_projects(()).projects == ()
    # Trust itself is NOT touched — the effective decision still passes.
    assert (
        effective_trust_decision(
            str(sub),
            {
                str(troot): {"hasTrustDialogAccepted": True},
                str(sub): {"hasTrustDialogAccepted": False},
            },
        )
        is TrustDecision.TRUSTED
    )


def test_scan_temp_root_does_not_cover_sibling(tmp_path, monkeypatch):
    # Segment boundary: a "t-ext" sibling of temp root "t" stays listed.
    troot = tmp_path / "t"
    sibling = tmp_path / "t-ext"
    sibling.mkdir(parents=True)
    membership = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(sibling): {"hasTrustDialogAccepted": True},
        },
        temp_roots={str(troot)},
    )

    assert {p.directory for p in membership.scan_projects(()).projects} == {
        str(sibling)
    }
