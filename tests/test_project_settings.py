"""Public project-settings read behavior (the ``~/.claude.json`` project map)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
    read_project_settings,
)


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


def test_project_settings_result_deeply_freezes_the_project_map():
    document = {
        "/work/app": {
            "hasTrustDialogAccepted": True,
            "metadata": {"tags": ["one"]},
        }
    }
    result = ProjectSettingsResult(ProjectSettingsState.AVAILABLE, document)

    document["/work/app"]["hasTrustDialogAccepted"] = False
    document["/work/app"]["metadata"]["tags"].append("later")

    published = result.projects["/work/app"]
    assert published["hasTrustDialogAccepted"] is True
    assert published["metadata"]["tags"] == ("one",)
    with pytest.raises(TypeError):
        result.projects["/work/other"] = {}
    with pytest.raises(TypeError):
        published["hasTrustDialogAccepted"] = False
    with pytest.raises(TypeError):
        published["metadata"]["tags"][0] = "changed"
