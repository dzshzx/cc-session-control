"""Bounded env-id capture cache for managed Remote Control panes.

The RC server prints its ``env_*`` id only to pane output. Stable panes keep a
positive result across refresh generations. Successful captures without an id
remain ordinary negative-cache misses. Capture failures use the same bounded
retry schedule but retain typed issues until a later successful capture or
window-generation invalidation.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .tmux import PaneCaptureResult, TmuxWindow

_ENV_ID_RE = re.compile(r"env_[A-Za-z0-9]+")
_INITIAL_BACKOFF = 10.0
_MAXIMUM_BACKOFF = 300.0

Capture = Callable[[str], PaneCaptureResult]
Clock = Callable[[], float]
CacheKey = tuple[str, int]


def extract_env_id(text: str) -> str:
    """Return the first valid ``env_*`` id in pane text, or ``""``."""
    match = _ENV_ID_RE.search(text)
    return match.group(0) if match else ""


@dataclass(frozen=True)
class EnvironmentIdIssue:
    """One pane-capture failure associated with a managed RC window."""

    source: str
    target: str
    path: str
    detail: str


@dataclass(frozen=True)
class EnvironmentIdResolution:
    """Resolved ids plus capture evidence for the current window generation."""

    environment_ids: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    issues: tuple[EnvironmentIdIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "environment_ids",
            MappingProxyType(dict(self.environment_ids)),
        )
        object.__setattr__(self, "issues", tuple(self.issues))


@dataclass(frozen=True)
class _Entry:
    env_id: str = ""
    retry_at: float = 0.0
    next_backoff: float = _INITIAL_BACKOFF
    issue: EnvironmentIdIssue | None = None


class EnvironmentIdCache:
    """Cache keyed only by tmux window identity and its current pane pid."""

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        initial_backoff: float = _INITIAL_BACKOFF,
        maximum_backoff: float = _MAXIMUM_BACKOFF,
    ) -> None:
        if initial_backoff <= 0:
            raise ValueError("initial_backoff must be positive")
        if maximum_backoff < initial_backoff:
            raise ValueError("maximum_backoff must be >= initial_backoff")
        self._clock = clock
        self._initial_backoff = initial_backoff
        self._maximum_backoff = maximum_backoff
        self._entries: dict[CacheKey, _Entry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def resolve_result(
        self,
        windows: Iterable[TmuxWindow],
        capture: Capture,
    ) -> EnvironmentIdResolution:
        """Resolve ids and retain capture failures across retry backoff."""
        current = tuple(windows)
        active = {
            (window.wid, window.pid) for window in current if window.pid is not None
        }
        self._entries = {
            key: entry for key, entry in self._entries.items() if key in active
        }

        now = self._clock()
        resolved: dict[str, str] = {}
        issues: list[EnvironmentIdIssue] = []
        for window in current:
            if window.pid is None:
                continue
            key = (window.wid, window.pid)
            entry = self._entries.get(key)
            if entry is not None and entry.env_id:
                resolved[window.wid] = entry.env_id
                continue
            if entry is not None and now < entry.retry_at:
                if entry.issue is not None:
                    issues.append(entry.issue)
                continue

            capture_result = capture(window.wid)
            if capture_result.target != window.wid:
                raise ValueError(
                    "pane capture result target does not match requested window",
                )
            if capture_result.issue is not None:
                capture_issue = EnvironmentIdIssue(
                    capture_result.issue.source,
                    window.wid,
                    window.path,
                    capture_result.issue.detail,
                )
                delay = (
                    entry.next_backoff if entry is not None else self._initial_backoff
                )
                self._entries[key] = _Entry(
                    retry_at=now + delay,
                    next_backoff=min(delay * 2, self._maximum_backoff),
                    issue=capture_issue,
                )
                issues.append(capture_issue)
                continue

            env_id = extract_env_id(capture_result.text)
            if env_id:
                self._entries[key] = _Entry(env_id=env_id)
                resolved[window.wid] = env_id
                continue

            delay = entry.next_backoff if entry is not None else self._initial_backoff
            self._entries[key] = _Entry(
                retry_at=now + delay,
                next_backoff=min(delay * 2, self._maximum_backoff),
            )
        return EnvironmentIdResolution(resolved, tuple(issues))

    def invalidate_window(self, window_id: str) -> None:
        """Forget every pane generation associated with one tmux window."""
        self._entries = {
            key: entry for key, entry in self._entries.items() if key[0] != window_id
        }

    def invalidate_all(self) -> None:
        """Forget all pane captures after an RC session lifecycle change."""
        self._entries.clear()
