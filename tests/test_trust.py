"""Tests for effective trust and path-keyed project membership.

`models.effective_trust` mirrors Claude Code's runtime trust-dialog gate
(verified against claude 2.1.218 on 2026-07-23): the dialog is suppressed when
the cwd or ANY ancestor holds a `hasTrustDialogAccepted: true` entry, and a
suppressed subdirectory gets an entry with an EXPLICIT False flag — while
declining the dialog writes no entry at all. So explicit False must never
veto, and ancestor matching must respect path-segment boundaries.
"""

from __future__ import annotations

from cc_session_control.models import effective_trust

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


# --- scan-level membership (path-keyed, inheritance-aware) ------------------

def _wire_scan(tmp_path, monkeypatch, projects, enabled=()):
    import json

    from cc_session_control.data import rc

    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"projects": projects}))
    monkeypatch.setattr(rc.cfg, "claude_json", cj)
    monkeypatch.setattr(rc, "list_enabled", lambda: list(enabled))
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [])
    return rc


def test_scan_includes_inherited_subdir(tmp_path, monkeypatch):
    # The original bug: parent trusted, real project created underneath —
    # the subdir entry carries the explicit-False footprint and must appear.
    parent = tmp_path / "parent"
    sub = parent / "new-proj"
    sub.mkdir(parents=True)
    rc = _wire_scan(tmp_path, monkeypatch, {
        str(parent): {"hasTrustDialogAccepted": True},
        str(sub): {"hasTrustDialogAccepted": False},
    })

    rows = {p.directory: p for p in rc.scan()}
    assert str(sub) in rows and str(parent) in rows
    assert rows[str(sub)].trusted is True
    assert rows[str(sub)].name == "new-proj"


def test_scan_excludes_untrusted_entry(tmp_path, monkeypatch):
    # Explicit False with NO trusted ancestor (the hapi shape) stays out.
    lone = tmp_path / "lone"
    lone.mkdir()
    rc = _wire_scan(tmp_path, monkeypatch, {
        str(lone): {"hasTrustDialogAccepted": False},
    })

    assert rc.scan() == []


# --- rc-enabled migration (legacy short names → absolute paths) -------------

def test_migrate_lines_resolves_against_legacy_root(monkeypatch):
    from cc_session_control.data import rc

    monkeypatch.setenv("CSCTL_WORKSPACE", "/srv/projects")
    out, changed = rc._migrate_lines(["# comment", "", "foo", "/abs/path", "a/b"])
    assert changed is True
    assert out == ["# comment", "", "/srv/projects/foo", "/abs/path",
                   "/srv/projects/a/b"]


def test_migrate_lines_idempotent():
    from cc_session_control.data import rc

    lines = ["# c", "/abs/one", "", "/abs/two"]
    out, changed = rc._migrate_lines(lines)
    assert changed is False
    assert out == lines


def test_list_enabled_migrates_rewrites_once_and_keeps_comments(
        tmp_path, monkeypatch):
    from cc_session_control.data import rc

    monkeypatch.setattr(rc.cfg, "config_dir", tmp_path)
    monkeypatch.setattr(rc.cfg, "rc_list", tmp_path / "rc-enabled")
    monkeypatch.setenv("CSCTL_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "rc-enabled").write_text("# keep me\nfoo\n/abs/bar\n")

    migrated_foo = str(tmp_path / "ws" / "foo")
    assert rc.list_enabled() == [migrated_foo, "/abs/bar"]
    content = (tmp_path / "rc-enabled").read_text()
    assert content == f"# keep me\n{migrated_foo}\n/abs/bar\n"
    assert rc.list_enabled() == [migrated_foo, "/abs/bar"]   # stable re-read


def test_list_rm_keeps_comments(tmp_path, monkeypatch):
    from cc_session_control.data import rc

    monkeypatch.setattr(rc.cfg, "config_dir", tmp_path)
    monkeypatch.setattr(rc.cfg, "rc_list", tmp_path / "rc-enabled")
    (tmp_path / "rc-enabled").write_text("# note\n/a\n/b\n")

    rc.list_rm("/a")
    assert (tmp_path / "rc-enabled").read_text() == "# note\n/b\n"
