"""Atomic refresh generation construction and handoff."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from ..models import AgentJob, RCProject, Session, SessionProc
from . import rc
from .cleanup import CleanupPlan, build_plan
from .snapshot import WorldSnapshot, build_world_snapshot


@dataclass(frozen=True)
class RefreshBatch:
    """One complete world generation ready for main-loop application."""

    generation: int
    snapshot: WorldSnapshot
    cleanup_plan: CleanupPlan
    cleanup_counts: Mapping[str, int]
    session_stats: Mapping[str, int]
    ordered_projects: tuple[RCProject, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cleanup_counts",
            MappingProxyType(dict(self.cleanup_counts)),
        )
        object.__setattr__(
            self,
            "session_stats",
            MappingProxyType(dict(self.session_stats)),
        )
        object.__setattr__(
            self,
            "ordered_projects",
            tuple(self.ordered_projects),
        )


@dataclass(frozen=True)
class RefreshFailure:
    """An expected source failure for one generation."""

    generation: int
    source: str
    detail: str


type RefreshResult = RefreshBatch | RefreshFailure
type RefreshBuilder = Callable[[int], RefreshResult]
type SnapshotBuilder = Callable[[], WorldSnapshot]
type CleanupBuilder = Callable[
    [
        list[Session],
        Sequence[SessionProc],
        AbstractSet[int],
        Sequence[AgentJob],
        Mapping[str, int | None],
    ],
    CleanupPlan,
]


def build_refresh_result(
    generation: int,
    *,
    snapshot_builder: SnapshotBuilder = build_world_snapshot,
    cleanup_builder: CleanupBuilder = build_plan,
) -> RefreshResult:
    """Build every view projection from one world snapshot.

    Expected local-source I/O failures become an explicit failed generation.
    Parser, invariant, and programming errors deliberately escape this boundary.
    """
    try:
        snapshot = snapshot_builder()
        plan = cleanup_builder(
            snapshot.sessions,
            snapshot.session_procs,
            snapshot.cur,
            snapshot.agent_jobs,
            snapshot.agents_map,
        )
    except OSError as exc:
        source = os.fspath(exc.filename) if exc.filename else "refresh sources"
        return RefreshFailure(generation, source, str(exc))

    counts = plan.counts()
    return RefreshBatch(
        generation=generation,
        snapshot=snapshot,
        cleanup_plan=plan,
        cleanup_counts=counts,
        session_stats={
            "total": len(snapshot.sessions),
            "empty": counts.get("empty", 0),
            "short": counts.get("short", 0),
            "orphans": counts.get("orphan_dirs", 0),
        },
        ordered_projects=tuple(
            rc.order_by_activity(snapshot.rc_projects, snapshot.sessions)
        ),
    )


class RequestResult(Enum):
    """Observable disposition of a refresh request."""

    STARTED = "started"
    COALESCED = "coalesced"
    CLOSED = "closed"


class RefreshCoordinator:
    """Publish complete generations through one request/consume interface."""

    def __init__(
        self,
        builder: RefreshBuilder,
        signal_ready: Callable[[], None],
    ) -> None:
        self._builder = builder
        self._signal_ready = signal_ready
        self._lock = threading.Lock()
        self._generation = 0
        self._running = False
        self._ready: RefreshResult | None = None
        self._refresh_again = False
        self._closed = False

    def request(self) -> RequestResult:
        """Start one generation unless work is already running or ready."""
        with self._lock:
            if self._closed:
                return RequestResult.CLOSED
            if self._running or self._ready is not None:
                self._refresh_again = True
                return RequestResult.COALESCED
            generation = self._claim_generation()
        self._start(generation)
        return RequestResult.STARTED

    def consume_latest(self) -> RefreshResult | None:
        """Take the one complete generation currently ready, if any."""
        generation: int | None = None
        with self._lock:
            result = self._ready
            self._ready = None
            if result is not None and self._refresh_again and not self._closed:
                self._refresh_again = False
                generation = self._claim_generation()
        if generation is not None:
            self._start(generation)
        return result

    def close(self) -> bool:
        """Stop accepting work and discard any result not yet consumed."""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._ready = None
            self._refresh_again = False
            return True

    def _claim_generation(self) -> int:
        """Reserve the next generation while ``_lock`` is held."""
        self._generation += 1
        self._running = True
        return self._generation

    def _start(self, generation: int) -> None:
        threading.Thread(
            target=self._build,
            args=(generation,),
            daemon=True,
        ).start()

    def _build(self, generation: int) -> None:
        completed = False
        try:
            result = self._builder(generation)
            completed = True
        finally:
            if not completed:
                with self._lock:
                    self._running = False
                    self._refresh_again = False
        with self._lock:
            if self._closed:
                self._running = False
                return
            self._ready = result
            self._running = False
            # Keep publish + signal ordered against close(): once close returns,
            # no callback can still be waiting to signal a discarded result.
            self._signal_ready()
