"""Typed access to the Claude project map stored in ``~/.claude.json``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType


class ProjectSettingsState(Enum):
    """Availability of the project map stored in ``~/.claude.json``."""

    AVAILABLE = "available"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    INVALID = "invalid"


def _freeze_project_value(value: object) -> object:
    """Copy one JSON-shaped project setting into immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_project_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_project_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_project_value(item) for item in value)
    return value


@dataclass(frozen=True)
class ProjectSettingsResult:
    """A project-map read whose external failure remains observable."""

    state: ProjectSettingsState
    projects: Mapping[str, Mapping[str, object]]
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "projects",
            MappingProxyType(
                {
                    path: MappingProxyType(
                        {
                            key: _freeze_project_value(value)
                            for key, value in project.items()
                        }
                    )
                    for path, project in self.projects.items()
                }
            ),
        )

    @property
    def available(self) -> bool:
        return self.state in {
            ProjectSettingsState.AVAILABLE,
            ProjectSettingsState.MISSING,
        }


def read_project_settings(path: Path) -> ProjectSettingsResult:
    """Read Claude's project map without conflating absence with failure."""

    try:
        with path.open(encoding="utf-8") as source:
            document = json.load(source)
    except FileNotFoundError:
        return ProjectSettingsResult(ProjectSettingsState.MISSING, {})
    except OSError as exc:
        return ProjectSettingsResult(
            ProjectSettingsState.UNREADABLE,
            {},
            str(exc),
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        return ProjectSettingsResult(
            ProjectSettingsState.MALFORMED,
            {},
            str(exc),
        )

    if not isinstance(document, dict):
        return ProjectSettingsResult(
            ProjectSettingsState.INVALID,
            {},
            "top-level JSON value is not an object",
        )
    projects = document.get("projects", {})
    if not isinstance(projects, dict):
        return ProjectSettingsResult(
            ProjectSettingsState.INVALID,
            {},
            "'projects' is not an object",
        )
    for project_path, project in projects.items():
        if not isinstance(project, dict):
            return ProjectSettingsResult(
                ProjectSettingsState.INVALID,
                {},
                f"project {project_path!r} is not an object",
            )
        if "hasTrustDialogAccepted" in project and not isinstance(
            project["hasTrustDialogAccepted"], bool
        ):
            return ProjectSettingsResult(
                ProjectSettingsState.INVALID,
                {},
                f"project {project_path!r} has a non-boolean trust flag",
            )
    return ProjectSettingsResult(ProjectSettingsState.AVAILABLE, projects)
