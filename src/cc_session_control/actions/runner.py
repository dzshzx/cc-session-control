"""Single-flight background execution for TUI actions and preparations.

The worker owns no urwid objects. It publishes a typed mutation result or
preparation value before signalling the App's pipe; only the main-loop pipe
callback consumes that output and applies UI effects.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ActionResult:
    """Data published by a worker and applied later on the main loop.

    The operator-facing outcome lives entirely in ``message`` (Chinese UI
    text); the App reads nothing else besides ``needs_refresh``.
    """

    message: str
    needs_refresh: bool = False


@dataclass(frozen=True)
class ActionCompletion[T]:
    """Typed worker data to be applied by a main-loop completion callback."""

    value: T


@dataclass(frozen=True)
class Accepted:
    action_key: str


@dataclass(frozen=True)
class Busy:
    active_key: str


@dataclass(frozen=True)
class Closed:
    pass


type SubmitResult = Accepted | Busy | Closed
type ActionOutput = ActionResult | ActionCompletion[object]
type Action = Callable[[], ActionOutput]


class _State(Enum):
    IDLE = "idle"
    RUNNING = "running"
    READY = "ready"
    CLOSED = "closed"


class ActionRunner:
    """Run at most one action or preparation until its output is consumed.

    Threads are daemonized, and ``close`` never joins: shutdown rejects new
    work immediately while an in-flight action may finish in the background.
    A late completion is discarded.  Unexpected exceptions are deliberately
    not caught, so Python routes them through ``threading.excepthook``; the
    ``finally`` path still publishes an empty completion and signals the pipe.
    Consumption then returns the runner to idle, preserving single-flight
    ordering even across a failed worker.
    """

    def __init__(self, signal_ready: Callable[[], None]) -> None:
        self._signal_ready = signal_ready
        self._lock = threading.Lock()
        self._state = _State.IDLE
        self._active_key = ""
        self._result: ActionOutput | None = None

    def submit(self, action_key: str, action: Action) -> SubmitResult:
        """Accept one action, or report the current busy/closed state."""
        with self._lock:
            if self._state is _State.CLOSED:
                return Closed()
            if self._state is not _State.IDLE:
                return Busy(self._active_key)
            self._state = _State.RUNNING
            self._active_key = action_key

        worker = threading.Thread(
            target=self._run,
            args=(action,),
            name=f"csctl-action-{action_key}",
            daemon=True,
        )
        worker.start()
        return Accepted(action_key)

    def _run(self, action: Action) -> None:
        published = False
        try:
            result = action()
            with self._lock:
                if self._state is not _State.CLOSED:
                    self._result = result
                    self._state = _State.READY
                    published = True
        finally:
            if not published:
                with self._lock:
                    if self._state is not _State.CLOSED:
                        self._result = None
                        self._state = _State.READY
            self._signal_ready()

    def consume_result(self) -> ActionOutput | None:
        """Consume the completed result once and release single-flight."""
        with self._lock:
            if self._state is not _State.READY:
                return None
            result = self._result
            self._result = None
            self._state = _State.IDLE
            self._active_key = ""
            return result

    def close(self) -> None:
        """Reject new work and discard pending/late results without joining."""
        with self._lock:
            self._state = _State.CLOSED
            self._active_key = ""
            self._result = None
