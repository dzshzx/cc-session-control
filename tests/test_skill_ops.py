"""Tests for the bundled-skill install/uninstall (`csctl skill`)."""

from cc_session_control.actions import skill_ops
from cc_session_control.config import cfg


def test_bundled_skill_text_is_packaged():
    text = skill_ops.bundled_skill_text()
    assert text.startswith("---")
    assert "name: claude-session-doctor" in text
    assert "csctl resume" in text


def test_install_uninstall_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    ok, msg = skill_ops.install()
    assert ok, msg
    target = tmp_path / "skills" / skill_ops.SKILL_NAME / "SKILL.md"
    assert target.is_file()
    assert "csctl" in target.read_text()

    ok, msg = skill_ops.uninstall()
    assert ok, msg
    assert not target.exists()

    ok, msg = skill_ops.uninstall()
    assert not ok  # second uninstall reports not installed


def test_install_refuses_existing_without_force(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    old = tmp_path / "skills" / skill_ops.SKILL_NAME
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("hand-maintained variant")
    (old / "scripts").mkdir()

    ok, msg = skill_ops.install()
    assert not ok and "--force" in msg
    assert (old / "SKILL.md").read_text() == "hand-maintained variant"

    ok, msg = skill_ops.install(force=True)
    assert ok
    assert not (old / "scripts").exists()  # replaced wholesale
    assert "csctl" in (old / "SKILL.md").read_text()
