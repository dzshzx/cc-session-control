"""Atomic refresh generation construction and handoff."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from ..models import Project, Session, format_inventory_issues
from . import membership
from .age_cleanup import AgeCleanupPlan, build_age_plan
from .cleanup import CleanupPlan, build_plan
from .liveness import LivenessSnapshot
from .removal import CleanupIssue
from .snapshot import WorldSnapshot, build_world_snapshot


@dataclass(frozen=True)
class RefreshBatch:
    """One complete world generation ready for main-loop application."""

    generation: int
    snapshot: WorldSnapshot
    cleanup_plan: CleanupPlan
    cleanup_counts: Mapping[str, int]
    session_stats: Mapping[str, int]
    ordered_projects: tuple[Project, ...]

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
    cleanup_plan: CleanupPlan = field(default_factory=CleanupPlan)

    def __post_init__(self) -> None:
        issue = CleanupIssue(source=self.source, error=self.detail)
        object.__setattr__(
            self,
            "cleanup_plan",
            replace(self.cleanup_plan, session_keyed_issue=issue),
        )

    @classmethod
    def with_age_plan(
        cls,
        generation: int,
        source: str,
        detail: str,
        age_plan: AgeCleanupPlan,
    ) -> RefreshFailure:
        """Map one failed generation to its only safe cleanup projection."""
        return cls(generation, source, detail, age_plan.to_cleanup_plan())


type RefreshResult = RefreshBatch | RefreshFailure
type RefreshBuilder = Callable[[int], RefreshResult]
type SnapshotBuilder = Callable[[], WorldSnapshot]
type AgeCleanupBuilder = Callable[[], AgeCleanupPlan]


class CleanupBuilder(Protocol):
    def __call__(
        self,
        sessions: Sequence[Session],
        evidence: LivenessSnapshot,
        *,
        transcript_sids: AbstractSet[str],
        age_plan: AgeCleanupPlan,
    ) -> CleanupPlan: ...


def build_refresh_result(
    generation: int,
    *,
    snapshot_builder: SnapshotBuilder = build_world_snapshot,
    age_builder: AgeCleanupBuilder = build_age_plan,
    cleanup_builder: CleanupBuilder = build_plan,
) -> RefreshResult:
    """Build every view projection from one world snapshot.

    Expected local-source I/O failures become an explicit failed generation.
    Parser, invariant, and programming errors deliberately escape this boundary.
    """
    age_plan = age_builder()

    def failure(source: str, detail: str) -> RefreshFailure:
        return RefreshFailure.with_age_plan(generation, source, detail, age_plan)

    try:
        snapshot = snapshot_builder()
        evidence = snapshot.liveness_snapshot
        if evidence is None:
            return failure(
                "liveness snapshot",
                "missing generation liveness evidence",
            )
        if not evidence.complete:
            first = evidence.issues[0]
            source = first.source
            if first.path:
                source += f" ({first.path})"
            detail = format_inventory_issues(evidence.issues)
            return failure(source, detail)
        transcript_scan = snapshot.transcript_scan
        if not transcript_scan.complete:
            transcript_issue = transcript_scan.issues[0]
            source = f"{transcript_issue.source} ({transcript_issue.path})"
            detail = format_inventory_issues(transcript_scan.issues)
            return failure(source, detail)
        # Cleanup models Claude state only (ADR-0005): non-Claude rows must
        # not enter the plan's session universe (their sids would neither
        # protect nor match any ~/.claude artifact). The pure-Claude common
        # case passes the generation tuple through UNCOPIED.
        claude_rows = snapshot.sessions
        if any(row.provider != "claude" for row in claude_rows):
            claude_rows = tuple(row for row in claude_rows if row.provider == "claude")
        plan = cleanup_builder(
            claude_rows,
            evidence,
            transcript_sids=transcript_scan.sids,
            age_plan=age_plan,
        )
    except OSError as exc:
        source = os.fspath(exc.filename) if exc.filename else "refresh sources"
        return failure(source, str(exc))

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
            membership.order_by_activity(snapshot.projects, snapshot.sessions)
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
