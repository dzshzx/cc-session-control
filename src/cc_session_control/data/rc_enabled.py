"""Locked transactions for the path-keyed ``rc-enabled`` list.

The lock, legacy migration, canonicalization, mutation, and atomic replacement
are one module so every caller observes one serialized generation.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")
_Mutation = Callable[[list[str]], tuple[list[str], _T]]


def _canonical(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def _path_lines(lines: list[str]) -> list[str]:
    return [
        line for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def migrate_lines(
    lines: list[str],
    legacy_root: Callable[[], str],
) -> tuple[list[str], bool]:
    """Canonicalize path rows once, retaining non-path rows and first-seen order."""

    migrated: list[str] = []
    seen: set[str] = set()
    root: str | None = None
    changed = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            migrated.append(raw)
            continue
        if os.path.isabs(stripped):
            path = _canonical(stripped)
        else:
            if root is None:
                root = legacy_root()
            path = _canonical(os.path.join(root, stripped))
        if path in seen:
            changed = True
            continue
        seen.add(path)
        migrated.append(path)
        changed = changed or path != raw
    return migrated, changed


class EnabledListStore:
    """Path-injected interface for serialized ``rc-enabled`` operations."""

    def __init__(
        self,
        path: Path,
        legacy_root: Callable[[], str],
    ) -> None:
        self._path = path
        self._legacy_root = legacy_root
        self._lock_path = path.with_name(f".{path.name}.lock")

    def list(self) -> list[str]:
        return self._update(lambda lines: (lines, _path_lines(lines)))

    def contains(self, path: str) -> bool:
        canonical = _canonical(path)
        return self._update(
            lambda lines: (lines, canonical in _path_lines(lines))
        )

    def add(self, path: str) -> bool:
        canonical = _canonical(path)

        def add_path(lines: list[str]) -> tuple[list[str], bool]:
            if canonical in _path_lines(lines):
                return lines, False
            return [*lines, canonical], True

        return self._update(add_path)

    def remove(self, path: str) -> bool:
        canonical = _canonical(path)

        def remove_path(lines: list[str]) -> tuple[list[str], bool]:
            updated = [line for line in lines if line != canonical]
            return updated, updated != lines

        return self._update(remove_path)

    def toggle(self, path: str) -> bool:
        canonical = _canonical(path)

        def toggle_path(lines: list[str]) -> tuple[list[str], bool]:
            if canonical in _path_lines(lines):
                return [line for line in lines if line != canonical], False
            return [*lines, canonical], True

        return self._update(toggle_path)

    def _update(self, mutate: _Mutation[_T]) -> _T:
        """The only read-modify-write seam; no operation bypasses its lock."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            self._ensure_file()
            lines = self._path.read_text(encoding="utf-8").splitlines()
            migrated, migration_changed = migrate_lines(
                lines,
                self._legacy_root,
            )
            updated, result = mutate(migrated)
            if migration_changed or updated != migrated:
                self._replace(updated)
            return result

    def _ensure_file(self) -> None:
        try:
            with self._path.open("xb"):
                pass
        except FileExistsError:
            pass

    def _replace(self, lines: list[str]) -> None:
        content = "".join(f"{line}\n" for line in lines)
        descriptor, name = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        temporary = Path(name)
        try:
            try:
                stream = os.fdopen(descriptor, "w", encoding="utf-8")
            except OSError:
                os.close(descriptor)
                raise
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except (OSError, UnicodeError):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
