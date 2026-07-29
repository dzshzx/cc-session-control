"""Tests for effective trust and path-keyed project membership.

`models.effective_trust` mirrors Claude Code's runtime trust-dialog gate
(verified against claude 2.1.218 on 2026-07-23): the dialog is suppressed when
the cwd or ANY ancestor holds a `hasTrustDialogAccepted: true` entry, and a
suppressed subdirectory gets an entry with an EXPLICIT False flag — while
declining the dialog writes no entry at all. So explicit False must never
veto, and ancestor matching must respect path-segment boundaries.
"""

from __future__ import annotations

from cc_session_control.models import (
    TrustDecision,
    effective_trust,
    effective_trust_decision,
)

WS = "/home/u/workspace"


def test_own_true_flag_is_trusted():
    projects = {WS + "/proj": {"hasTrustDialogAccepted": True}}
    assert effective_trust(WS + "/proj", projects) is True


def test_no_entry_no_ancestor_is_untrusted():
    projects = {WS + "/proj": {"hasTrustDialogAccepted": True}}
    assert effective_trust("/srv/other", projects) is False


def test_missing_flag_without_trusted_ancestor_is_untrusted():
    projects = {WS + "/proj": {}}
    assert effective_trust(WS + "/proj", projects) is False


def test_explicit_false_without_trusted_ancestor_is_untrusted():
    projects = {WS + "/proj": {"hasTrustDialogAccepted": False}}
    assert effective_trust(WS + "/proj", projects) is False


def test_ancestor_true_inherits_to_subdir():
    # The claude-2.1.218 shape: parent accepted, subdir entry explicit False.
    projects = {
        WS: {"hasTrustDialogAccepted": True},
        WS + "/new-proj": {"hasTrustDialogAccepted": False},
    }
    assert effective_trust(WS + "/new-proj", projects) is True


def test_ancestor_true_inherits_without_own_entry():
    projects = {WS: {"hasTrustDialogAccepted": True}}
    assert effective_trust(WS + "/brand-new", projects) is True


def test_ancestor_matches_any_depth():
    projects = {"/home/u": {"hasTrustDialogAccepted": True}}
    assert effective_trust(WS + "/deep/nested/dir", projects) is True


def test_prefix_collision_is_not_an_ancestor():
    # /a/workspace must NOT cover /a/workspace-external (segment boundary).
    projects = {WS: {"hasTrustDialogAccepted": True}}
    assert effective_trust(WS + "-external/proj", projects) is False


def test_root_trusted_covers_everything():
    # rstrip guard: root="/" must not degenerate to a never-matching "//".
    projects = {"/": {"hasTrustDialogAccepted": True}}
    assert effective_trust("/any/dir", projects) is True


def test_trailing_slash_keys_normalize():
    projects = {WS + "/": {"hasTrustDialogAccepted": True}}
    assert effective_trust(WS + "/proj", projects) is True
    assert effective_trust(WS, projects) is True


def test_false_ancestor_does_not_inherit():
    projects = {
        WS: {"hasTrustDialogAccepted": False},
        WS + "/proj": {"hasTrustDialogAccepted": False},
    }
    assert effective_trust(WS + "/proj", projects) is False


def test_non_dict_entry_is_ignored():
    projects = {WS: None, WS + "/proj": "garbage"}
    assert effective_trust(WS + "/proj", projects) is False


def test_empty_inputs():
    assert effective_trust("", {WS: {"hasTrustDialogAccepted": True}}) is False
    assert effective_trust(WS, {}) is False


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
    assert effective_trust(WS + "/proj", None) is False


# --- scan-level membership (path-keyed, inheritance-aware) ------------------


def _wire_scan(tmp_path, monkeypatch, projects, enabled=(), temp_roots=()):
    import json
    import os

    from cc_session_control.data import rc

    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"projects": projects}))
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    monkeypatch.setattr(rc, "list_enabled", lambda: list(enabled))
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: rc.tmux.WindowInventory(),
    )
    # pytest tmp_path lives under the REAL platform temp root, so the temp-dir
    # membership filter is neutralized unless a test injects roots explicitly.
    monkeypatch.setattr(
        rc, "_TEMP_ROOTS", frozenset(os.path.normpath(p) for p in temp_roots)
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

    rows = {p.directory: p for p in rc.scan()}
    assert str(sub) in rows and str(parent) in rows
    assert rows[str(sub)].trusted is True
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

    assert rc.scan() == []


def test_scan_and_start_keep_unavailable_trust_distinct_and_fail_closed(
    tmp_path,
    monkeypatch,
):
    from cc_session_control.data import rc

    project = tmp_path / "app"
    project.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{broken")
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(rc, "list_enabled", lambda: [str(project)])
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: rc.tmux.WindowInventory(),
    )
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())
    launches = []
    monkeypatch.setattr(
        rc.tmux,
        "run_in_tmux_result",
        lambda *args, **kwargs: launches.append(args),
    )

    scan_result = rc.scan_result()
    start_result = rc.start_one_result(str(project))

    assert scan_result.settings.state.value == "malformed"
    assert scan_result.projects[0].trust_decision is TrustDecision.UNAVAILABLE
    assert scan_result.projects[0].trusted is False
    assert start_result.state is rc.StartState.TRUST_UNAVAILABLE
    assert launches == []


# --- temp-dir membership filter (trust untouched, discovery only) -----------


def test_is_temp_path_segment_boundary(monkeypatch):
    from cc_session_control.data import rc

    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset({"/tmp"}))
    assert rc._is_temp_path("/tmp") is True
    assert rc._is_temp_path("/tmp/x/y") is True
    assert rc._is_temp_path("/tmpfoo") is False
    assert rc._is_temp_path("/home/u/tmp") is False


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

    assert rc.scan() == []
    # Trust itself is NOT touched — the start gate still passes.
    assert rc.is_trusted(str(sub)) is True


def test_scan_keeps_enabled_temp_project(tmp_path, monkeypatch):
    troot = tmp_path / "t"
    sub = troot / "demo"
    sub.mkdir(parents=True)
    rc = _wire_scan(
        tmp_path,
        monkeypatch,
        {
            str(troot): {"hasTrustDialogAccepted": True},
        },
        enabled=(str(sub),),
        temp_roots={str(troot)},
    )

    rows = {p.directory for p in rc.scan()}
    assert rows == {str(sub)}


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

    rows = {p.directory: p for p in rc.scan()}
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

    assert {p.directory for p in rc.scan()} == {str(sibling)}


# --- rc-enabled migration (legacy short names → absolute paths) -------------


def test_migrate_lines_resolves_against_legacy_root(monkeypatch):
    from cc_session_control.data import rc

    monkeypatch.setenv("CSCTL_WORKSPACE", "/srv/projects")
    out, changed = rc._migrate_lines(["# comment", "", "foo", "/abs/path", "a/b"])
    assert changed is True
    assert out == [
        "# comment",
        "",
        "/srv/projects/foo",
        "/abs/path",
        "/srv/projects/a/b",
    ]


def test_migrate_lines_idempotent():
    from cc_session_control.data import rc

    lines = ["# c", "/abs/one", "", "/abs/two"]
    out, changed = rc._migrate_lines(lines)
    assert changed is False
    assert out == lines


def test_list_enabled_migrates_rewrites_once_and_keeps_comments(tmp_path, monkeypatch):
    from cc_session_control.data import rc

    monkeypatch.setattr(rc.cfg, "config_dir", tmp_path)
    monkeypatch.setattr(rc.cfg, "rc_list", tmp_path / "rc-enabled")
    monkeypatch.setenv("CSCTL_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "rc-enabled").write_text("# keep me\nfoo\n/abs/bar\n")

    migrated_foo = str(tmp_path / "ws" / "foo")
    assert rc.list_enabled() == [migrated_foo, "/abs/bar"]
    content = (tmp_path / "rc-enabled").read_text()
    assert content == f"# keep me\n{migrated_foo}\n/abs/bar\n"
    assert rc.list_enabled() == [migrated_foo, "/abs/bar"]  # stable re-read


def test_list_rm_keeps_comments(tmp_path, monkeypatch):
    from cc_session_control.data import rc

    monkeypatch.setattr(rc.cfg, "config_dir", tmp_path)
    monkeypatch.setattr(rc.cfg, "rc_list", tmp_path / "rc-enabled")
    (tmp_path / "rc-enabled").write_text("# note\n/a\n/b\n")

    rc.list_rm("/a")
    assert (tmp_path / "rc-enabled").read_text() == "# note\n/b\n"
