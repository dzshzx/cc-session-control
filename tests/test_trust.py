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

    from cc_session_control.data import membership, rc

    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"projects": projects}))
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    # Isolate the other ADR-0007 membership sources from the real machine:
    # codex/kimi trust stores and the curation file.
    monkeypatch.setattr(rc.cfg, "providers", ("claude",))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: rc.tmux.WindowInventory(),
    )
    # pytest tmp_path lives under the REAL platform temp root, so the temp-dir
    # membership filter is neutralized unless a test injects roots explicitly.
    monkeypatch.setattr(
        membership,
        "_TEMP_ROOTS",
        frozenset(os.path.normpath(p) for p in temp_roots),
    )
    return rc


def test_scan_includes_inherited_subdir(tmp_path, monkeypatch):
    # The original bug: parent trusted, real project created underneath —
    # the subdir entry carries the explicit-False footprint and must appear.
    parent = tmp_path / "parent"
    sub = parent / "new-proj"
    sub.mkdir(parents=True)
    rc = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(parent): {"hasTrustDialogAccepted": True},
            str(sub): {"hasTrustDialogAccepted": False},
        },
    )

    rows = {p.directory: p for p in rc.scan_result().projects}
    assert str(sub) in rows and str(parent) in rows
    assert rows[str(sub)].trust_decision is TrustDecision.TRUSTED
    assert rows[str(sub)].name == "new-proj"


def test_scan_excludes_untrusted_entry(tmp_path, monkeypatch):
    # Explicit False with NO trusted ancestor (the hapi shape) stays out.
    lone = tmp_path / "lone"
    lone.mkdir()
    rc = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(lone): {"hasTrustDialogAccepted": False},
        },
    )

    assert rc.scan_result().projects == []


def test_scan_and_start_keep_unavailable_trust_distinct_and_fail_closed(
    tmp_path,
    monkeypatch,
):
    from cc_session_control.data import membership, rc

    project = tmp_path / "app"
    project.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{broken")
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(rc.cfg, "providers", ("claude",))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: rc.tmux.WindowInventory(),
    )
    monkeypatch.setattr(membership, "_TEMP_ROOTS", frozenset())
    launches = []
    monkeypatch.setattr(
        rc.tmux,
        "run_in_tmux_result",
        lambda *args, **kwargs: launches.append(args),
    )

    scan_result = rc.scan_result()
    start_result = rc.start_one_result(str(project))

    # Membership fails closed (no trusted set to enumerate), but the typed
    # settings evidence stays observable and the start gate refuses to spawn.
    assert scan_result.settings.state.value == "malformed"
    assert scan_result.projects == []
    assert start_result.state is rc.StartState.TRUST_UNAVAILABLE
    assert launches == []


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
    rc = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(troot): {"hasTrustDialogAccepted": True},
            str(sub): {"hasTrustDialogAccepted": False},
        },
        temp_roots={str(troot)},
    )

    assert rc.scan_result().projects == []
    # Trust itself is NOT touched — the start gate still passes.
    assert rc.project_trust(str(sub)).decision is TrustDecision.TRUSTED


def test_scan_keeps_temp_project_with_rc_window(tmp_path, monkeypatch):
    from cc_session_control.data import tmux

    troot = tmp_path / "t"
    sub = troot / "served"
    sub.mkdir(parents=True)
    # A server's claude leaves the suppressed-False footprint, so the path
    # IS enumerated (windows join rows, they don't create membership).
    rc = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(troot): {"hasTrustDialogAccepted": True},
            str(sub): {"hasTrustDialogAccepted": False},
        },
        temp_roots={str(troot)},
    )
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: tmux.WindowInventory(
            (
                tmux.TmuxWindow(
                    wid="@1",
                    name="served",
                    dead=False,
                    pid=42,
                    path=str(sub),
                ),
            )
        ),
    )

    rows = {p.directory: p for p in rc.scan_result().projects}
    assert set(rows) == {str(sub)}
    assert rows[str(sub)].status == "running"


def test_scan_temp_root_does_not_cover_sibling(tmp_path, monkeypatch):
    # Segment boundary: a "t-ext" sibling of temp root "t" stays listed.
    troot = tmp_path / "t"
    sibling = tmp_path / "t-ext"
    sibling.mkdir(parents=True)
    rc = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(sibling): {"hasTrustDialogAccepted": True},
        },
        temp_roots={str(troot)},
    )

    assert {p.directory for p in rc.scan_result().projects} == {str(sibling)}
