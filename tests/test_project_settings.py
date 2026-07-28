"""Public project-settings and trust-result behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_session_control.data import project_settings
from cc_session_control.data.project_settings import (
    ProjectSettingsState,
    SettingWriteFailure,
    SettingWriteState,
    read_project_settings,
    write_rc_at_startup,
)
from cc_session_control.models import RCStartupSettingState


def test_missing_claude_json_is_an_empty_non_failure(tmp_path):
    result = read_project_settings(tmp_path / ".claude.json")

    assert result.state is ProjectSettingsState.MISSING
    assert result.projects == {}
    assert result.available is True


def test_valid_claude_json_exposes_the_project_map(tmp_path):
    path = tmp_path / ".claude.json"
    projects = {"/work/app": {"hasTrustDialogAccepted": True}}
    path.write_text(json.dumps({"projects": projects, "other": 1}))

    result = read_project_settings(path)

    assert result.state is ProjectSettingsState.AVAILABLE
    assert result.projects == projects
    assert result.available is True


def test_malformed_claude_json_is_unavailable(tmp_path):
    path = tmp_path / ".claude.json"
    path.write_text("{broken")

    result = read_project_settings(path)

    assert result.state is ProjectSettingsState.MALFORMED
    assert result.projects == {}
    assert result.available is False


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"projects": []},
        {"projects": None},
        {"projects": {"/work/app": []}},
        {
            "projects": {
                "/work/app": {"hasTrustDialogAccepted": "yes"},
            },
        },
        {
            "projects": {
                "/work/app": {"hasTrustDialogAccepted": None},
            },
        },
    ],
)
def test_non_object_project_schema_is_unavailable(tmp_path, document):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps(document))

    result = read_project_settings(path)

    assert result.state is ProjectSettingsState.INVALID
    assert result.projects == {}
    assert result.available is False


def test_unreadable_claude_json_is_observable_without_chmod_assumptions(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / ".claude.json"
    path.write_text("{}")

    def deny_open(self: Path, *args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "open", deny_open)
    result = read_project_settings(path)

    assert result.state is ProjectSettingsState.UNREADABLE
    assert "permission denied" in result.detail
    assert result.available is False


def test_rc_setting_read_uses_base_when_local_is_missing(tmp_path):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"remoteControlAtStartup": True}))

    result = project_settings.read_rc_at_startup(project)

    assert result.state is RCStartupSettingState.TRUE
    assert result.value is True
    assert result.source == settings
    assert result.available is True


@pytest.mark.parametrize(
    ("local_document", "expected_state", "expected_value"),
    [
        (None, RCStartupSettingState.MISSING, None),
        ({}, RCStartupSettingState.UNSET, None),
        ({"remoteControlAtStartup": True}, RCStartupSettingState.TRUE, True),
        ({"remoteControlAtStartup": False}, RCStartupSettingState.FALSE, False),
    ],
)
def test_rc_setting_read_distinguishes_normal_states(
    tmp_path,
    local_document,
    expected_state,
    expected_value,
):
    project = tmp_path / "app"
    local = project / ".claude" / "settings.local.json"
    if local_document is not None:
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps(local_document))

    result = project_settings.read_rc_at_startup(project)

    assert result.state is expected_state
    assert result.value is expected_value
    assert result.available is True
    assert result.source == (local if local_document is not None else None)


def test_rc_setting_read_unset_local_continues_to_base(tmp_path):
    project = tmp_path / "app"
    settings_dir = project / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.local.json").write_text("{}")
    base = settings_dir / "settings.json"
    base.write_text(json.dumps({"remoteControlAtStartup": False}))

    result = project_settings.read_rc_at_startup(project)

    assert result.state is RCStartupSettingState.FALSE
    assert result.value is False
    assert result.source == base


def test_rc_setting_read_local_bool_takes_precedence_over_base(tmp_path):
    project = tmp_path / "app"
    settings_dir = project / ".claude"
    settings_dir.mkdir(parents=True)
    local = settings_dir / "settings.local.json"
    local.write_text(json.dumps({"remoteControlAtStartup": False}))
    (settings_dir / "settings.json").write_text(
        json.dumps({"remoteControlAtStartup": True})
    )

    result = project_settings.read_rc_at_startup(project)

    assert result.state is RCStartupSettingState.FALSE
    assert result.value is False
    assert result.source == local


@pytest.mark.parametrize(
    ("raw", "expected_state", "detail"),
    [
        (b"{broken", RCStartupSettingState.MALFORMED, ""),
        (b"\xff", RCStartupSettingState.MALFORMED, ""),
        (b"[]", RCStartupSettingState.INVALID, "top-level"),
        (
            b'{"remoteControlAtStartup": "yes"}',
            RCStartupSettingState.INVALID,
            "not a boolean",
        ),
    ],
)
def test_rc_setting_read_does_not_fallback_past_broken_local_file(
    tmp_path,
    raw,
    expected_state,
    detail,
):
    project = tmp_path / "app"
    settings_dir = project / ".claude"
    settings_dir.mkdir(parents=True)
    local = settings_dir / "settings.local.json"
    local.write_bytes(raw)
    (settings_dir / "settings.json").write_text(
        json.dumps({"remoteControlAtStartup": True})
    )

    result = project_settings.read_rc_at_startup(project)

    assert result.state is expected_state
    assert result.value is None
    assert result.source == local
    assert detail in result.detail
    assert result.available is False


def test_rc_setting_read_does_not_fallback_past_unreadable_local_file(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "app"
    settings_dir = project / ".claude"
    settings_dir.mkdir(parents=True)
    local = settings_dir / "settings.local.json"
    local.write_text("{}")
    (settings_dir / "settings.json").write_text(
        json.dumps({"remoteControlAtStartup": True})
    )
    read_bytes = Path.read_bytes

    def deny_local(self):
        if self == local:
            raise PermissionError("permission denied")
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", deny_local)

    result = project_settings.read_rc_at_startup(project)

    assert result.state is RCStartupSettingState.UNREADABLE
    assert result.source == local
    assert "permission denied" in result.detail
    assert result.available is False


def test_rc_setting_write_creates_file_preserves_keys_and_reports_unchanged(
    tmp_path,
):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}}) + "\n")

    changed = write_rc_at_startup(project, True)
    first_bytes = settings.read_bytes()
    unchanged = write_rc_at_startup(project, True)

    assert changed.state is SettingWriteState.UPDATED
    assert json.loads(first_bytes) == {
        "permissions": {"allow": ["Read"]},
        "remoteControlAtStartup": True,
    }
    assert unchanged.state is SettingWriteState.UNCHANGED
    assert settings.read_bytes() == first_bytes


def test_rc_setting_write_creates_missing_settings_file(tmp_path):
    project = tmp_path / "new-app"

    result = write_rc_at_startup(project, False)

    assert result.state is SettingWriteState.UPDATED
    assert json.loads(result.path.read_text()) == {
        "remoteControlAtStartup": False,
    }


def test_rc_setting_write_can_remove_override_without_losing_other_keys(
    tmp_path,
):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "remoteControlAtStartup": False,
                "keep": {"nested": 1},
            }
        )
    )

    result = write_rc_at_startup(project, None)

    assert result.state is SettingWriteState.UPDATED
    assert json.loads(settings.read_text()) == {"keep": {"nested": 1}}


@pytest.mark.parametrize("raw", [b"{broken", b"[]"])
def test_rc_setting_write_refuses_invalid_existing_json_without_overwrite(
    tmp_path,
    raw,
):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(raw)

    result = write_rc_at_startup(project, True)

    assert result.state is SettingWriteState.FAILED
    assert result.failure in {
        SettingWriteFailure.MALFORMED,
        SettingWriteFailure.INVALID,
    }
    assert settings.read_bytes() == raw


def test_rc_setting_lock_failure_is_typed_and_preserves_original(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    original = b'{"keep": true}\n'
    settings.write_bytes(original)

    def fail_lock(*args, **kwargs):
        raise OSError("lock unavailable")

    monkeypatch.setattr(project_settings.fcntl, "flock", fail_lock)
    result = write_rc_at_startup(project, True)

    assert result.failure is SettingWriteFailure.LOCK
    assert settings.read_bytes() == original


def test_rc_setting_read_failure_is_typed_and_preserves_original(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    original = b'{"keep": true}\n'
    settings.write_bytes(original)
    real_read_bytes = Path.read_bytes

    def fail_read(self):
        if self == settings:
            raise PermissionError("read denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    result = write_rc_at_startup(project, True)

    assert result.failure is SettingWriteFailure.READ
    assert real_read_bytes(settings) == original


def test_rc_setting_temp_write_failure_preserves_original_and_cleans_tmp(
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    original = b'{"keep": true}\n'
    settings.write_bytes(original)

    def fail_temp(*args, **kwargs):
        raise OSError("temporary write denied")

    monkeypatch.setattr(
        project_settings.tempfile,
        "NamedTemporaryFile",
        fail_temp,
    )
    result = write_rc_at_startup(project, True)

    assert result.failure is SettingWriteFailure.WRITE
    assert settings.read_bytes() == original
    assert list(settings.parent.glob(".settings.local.json.*.tmp")) == []


@pytest.mark.parametrize(
    ("boundary", "failure"),
    [
        ("fsync", SettingWriteFailure.FSYNC),
        ("replace", SettingWriteFailure.REPLACE),
    ],
)
def test_rc_setting_atomic_boundary_failure_preserves_bytes_and_cleans_tmp(
    tmp_path,
    monkeypatch,
    boundary,
    failure,
):
    project = tmp_path / "app"
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    original = b'{"keep": true}\n'
    settings.write_bytes(original)

    def fail(*args, **kwargs):
        raise OSError(f"{boundary} unavailable")

    monkeypatch.setattr(project_settings.os, boundary, fail)
    result = write_rc_at_startup(project, True)

    assert result.failure is failure
    assert settings.read_bytes() == original
    assert list(settings.parent.glob(".settings.local.json.*.tmp")) == []
