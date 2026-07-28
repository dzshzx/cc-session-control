"""Typed access to Claude project metadata and per-project settings."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Any


class ProjectSettingsState(Enum):
    """Availability of the project map stored in ``~/.claude.json``."""

    AVAILABLE = "available"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProjectSettingsResult:
    """A project-map read whose external failure remains observable."""

    state: ProjectSettingsState
    projects: dict[str, Any]
    detail: str = ""

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
    try:
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
    except OSError as exc:
        return _write_failure(path, SettingWriteFailure.WRITE, str(exc))
    temporary_path = Path(temporary.name)

    try:
        json.dump(document, temporary, indent=2)
        temporary.write("\n")
        temporary.flush()
    except OSError as exc:
        return _cleanup_temporary(
            path,
            temporary_path,
            temporary,
            _write_failure(path, SettingWriteFailure.WRITE, str(exc)),
        )

    try:
        os.fsync(temporary.fileno())
    except OSError as exc:
        return _cleanup_temporary(
            path,
            temporary_path,
            temporary,
            _write_failure(path, SettingWriteFailure.FSYNC, str(exc)),
        )

    try:
        temporary.close()
    except OSError as exc:
        return _cleanup_temporary(
            path,
            temporary_path,
            temporary,
            _write_failure(path, SettingWriteFailure.WRITE, str(exc)),
        )

    try:
        os.replace(temporary_path, path)
    except OSError as exc:
        return _cleanup_temporary(
            path,
            temporary_path,
            temporary,
            _write_failure(path, SettingWriteFailure.REPLACE, str(exc)),
        )
    return SettingWriteResult(SettingWriteState.UPDATED, path)


def _cleanup_temporary(
    path: Path,
    temporary_path: Path,
    temporary: IO[str],
    outcome: SettingWriteResult,
) -> SettingWriteResult:
    """Remove a failed write's temporary file and expose cleanup failures."""

    cleanup_errors: list[str] = []
    if not temporary.closed:
        try:
            temporary.close()
        except OSError as exc:
            cleanup_errors.append(f"close: {exc}")
    try:
        temporary_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        cleanup_errors.append(f"unlink: {exc}")
    if cleanup_errors:
        original_failure = (
            outcome.failure.value
            if outcome.failure is not None
            else outcome.state.value
        )
        return _write_failure(
            path,
            SettingWriteFailure.CLEANUP,
            f"{original_failure}: {outcome.detail}; " + "; ".join(cleanup_errors),
        )
    return outcome


def write_rc_at_startup(
    directory: str | Path,
    value: bool | None,
) -> SettingWriteResult:
    """Atomically update one project setting under a dedicated file lock."""

    settings_dir = Path(directory) / ".claude"
    path = settings_dir / "settings.local.json"
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _write_failure(
            path,
            SettingWriteFailure.CREATE_DIRECTORY,
            str(exc),
        )

    lock_path = settings_dir / ".settings.local.json.lock"
    try:
        lock_file = lock_path.open("a", encoding="utf-8")
    except OSError as exc:
        return _write_failure(path, SettingWriteFailure.LOCK, str(exc))

    with lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            return _write_failure(path, SettingWriteFailure.LOCK, str(exc))

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
