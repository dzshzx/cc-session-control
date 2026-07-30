"""Typed access to Claude project metadata and per-project settings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..models import RCStartupSettingRead, RCStartupSettingState
from .atomic_write import (
    AdvisoryLockError,
    AdvisoryLockStage,
    AtomicWriteError,
    AtomicWriteStage,
    advisory_lock,
    atomic_replace,
)


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


class SettingWriteState(Enum):
    """Outcome of changing ``remoteControlAtStartup``."""

    UPDATED = "updated"
    UNCHANGED = "unchanged"
    FAILED = "failed"


class SettingWriteFailure(Enum):
    """External boundary that prevented a settings update."""

    CREATE_DIRECTORY = "create-directory"
    LOCK = "lock"
    READ = "read"
    MALFORMED = "malformed"
    INVALID = "invalid"
    WRITE = "write"
    FSYNC = "fsync"
    REPLACE = "replace"
    CLEANUP = "cleanup"
    UNLOCK = "unlock"


@dataclass(frozen=True)
class SettingWriteResult:
    """Typed, operator-visible result of an atomic settings write."""

    state: SettingWriteState
    path: Path
    failure: SettingWriteFailure | None = None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.state is not SettingWriteState.FAILED


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


def _read_rc_at_startup_file(path: Path) -> RCStartupSettingRead:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return RCStartupSettingRead(RCStartupSettingState.MISSING, path)
    except OSError as exc:
        return RCStartupSettingRead(
            RCStartupSettingState.UNREADABLE,
            path,
            str(exc),
        )

    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        return RCStartupSettingRead(
            RCStartupSettingState.MALFORMED,
            path,
            str(exc),
        )
    if not isinstance(document, dict):
        return RCStartupSettingRead(
            RCStartupSettingState.INVALID,
            path,
            "top-level JSON value is not an object",
        )
    if "remoteControlAtStartup" not in document:
        return RCStartupSettingRead(RCStartupSettingState.UNSET, path)
    value = document["remoteControlAtStartup"]
    if not isinstance(value, bool):
        return RCStartupSettingRead(
            RCStartupSettingState.INVALID,
            path,
            "'remoteControlAtStartup' is not a boolean",
        )
    state = RCStartupSettingState.TRUE if value else RCStartupSettingState.FALSE
    return RCStartupSettingRead(state, path)


def read_rc_at_startup(directory: str | Path) -> RCStartupSettingRead:
    """Read the effective startup setting without bypassing broken evidence."""

    settings_dir = Path(directory) / ".claude"
    first_unset: RCStartupSettingRead | None = None
    for name in ("settings.local.json", "settings.json"):
        result = _read_rc_at_startup_file(settings_dir / name)
        if result.state is RCStartupSettingState.MISSING:
            continue
        if result.state is RCStartupSettingState.UNSET:
            if first_unset is None:
                first_unset = result
            continue
        return result
    if first_unset is not None:
        return first_unset
    return RCStartupSettingRead(RCStartupSettingState.MISSING)


_STAGE_TO_FAILURE: Mapping[AtomicWriteStage, SettingWriteFailure] = {
    AtomicWriteStage.CREATE: SettingWriteFailure.WRITE,
    AtomicWriteStage.WRITE: SettingWriteFailure.WRITE,
    AtomicWriteStage.FSYNC: SettingWriteFailure.FSYNC,
    AtomicWriteStage.REPLACE: SettingWriteFailure.REPLACE,
    AtomicWriteStage.CLEANUP: SettingWriteFailure.CLEANUP,
}


def _write_failure(
    path: Path,
    failure: SettingWriteFailure,
    detail: str,
) -> SettingWriteResult:
    return SettingWriteResult(SettingWriteState.FAILED, path, failure, detail)


def _load_settings_for_write(path: Path) -> dict[str, Any] | SettingWriteResult:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        return _write_failure(path, SettingWriteFailure.READ, str(exc))

    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        return _write_failure(path, SettingWriteFailure.MALFORMED, str(exc))
    if not isinstance(document, dict):
        return _write_failure(
            path,
            SettingWriteFailure.INVALID,
            "top-level JSON value is not an object",
        )
    return document


def _replace_settings(
    path: Path,
    document: dict[str, Any],
) -> SettingWriteResult:
    content = json.dumps(document, indent=2) + "\n"
    try:
        atomic_replace(path, content)
    except AtomicWriteError as exc:
        return _write_failure(path, _STAGE_TO_FAILURE[exc.stage], exc.detail)
    return SettingWriteResult(SettingWriteState.UPDATED, path)


_LOCK_STAGE_TO_FAILURE: Mapping[AdvisoryLockStage, SettingWriteFailure] = {
    AdvisoryLockStage.LOCK: SettingWriteFailure.LOCK,
    AdvisoryLockStage.UNLOCK: SettingWriteFailure.UNLOCK,
}


def _locked_write(path: Path, value: bool | None) -> SettingWriteResult:
    document = _load_settings_for_write(path)
    if isinstance(document, SettingWriteResult):
        return document

    updated = dict(document)
    if value is None:
        updated.pop("remoteControlAtStartup", None)
    else:
        updated["remoteControlAtStartup"] = value
    if updated == document:
        return SettingWriteResult(SettingWriteState.UNCHANGED, path)
    return _replace_settings(path, updated)


def write_rc_at_startup(
    directory: str | Path,
    value: bool | None,
) -> SettingWriteResult:
    """Atomically update one project setting under a dedicated file lock."""

    settings_dir = Path(directory) / ".claude"
    path = settings_dir / "settings.local.json"
    try:
        settings_dir.mkdir(exist_ok=True)
    except OSError as exc:
        return _write_failure(
            path,
            SettingWriteFailure.CREATE_DIRECTORY,
            str(exc),
        )

    lock_path = settings_dir / ".settings.local.json.lock"
    outcome: SettingWriteResult | None = None
    try:
        with advisory_lock(lock_path):
            outcome = _locked_write(path, value)
    except AdvisoryLockError as exc:
        detail = exc.detail
        if outcome is not None and not outcome.success:
            previous = (
                outcome.failure.value
                if outcome.failure is not None
                else outcome.state.value
            )
            detail = f"{previous}: {outcome.detail}; unlock: {detail}"
        return _write_failure(path, _LOCK_STAGE_TO_FAILURE[exc.stage], detail)
    if outcome is None:
        raise RuntimeError("project-settings transaction produced no result")
    return outcome
