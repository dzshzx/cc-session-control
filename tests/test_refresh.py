"""Atomic refresh generations at the coordinator's public seam."""

import threading
from dataclasses import FrozenInstanceError
from queue import Empty, Queue
from threading import Event, ExceptHookArgs, Thread

import pytest

from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.refresh import (
    RefreshBatch,
    RefreshCoordinator,
    RefreshFailure,
    RequestResult,
    build_refresh_result,
)
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import RCProject, Session


def _batch(generation: int) -> RefreshBatch:
    return RefreshBatch(
        generation=generation,
        snapshot=WorldSnapshot(),
        cleanup_plan=CleanupPlan(),
        cleanup_counts={},
        session_stats={},
        ordered_projects=(),
    )


def _marked_batch(generation: int) -> RefreshBatch:
    session = Session(
        sid=f"gen-{generation}",
        cwd="/tmp/project",
        label=f"generation {generation}",
        mtime=float(generation),
        prompts=0,
        pid=None,
        alive=False,
        current=False,
    )
    return RefreshBatch(
        generation=generation,
        snapshot=WorldSnapshot(sessions=[session]),
        cleanup_plan=CleanupPlan(empty=[session]),
        cleanup_counts={"empty": 1},
        session_stats={"total": 1, "empty": 1},
        ordered_projects=(),
    )


def test_complete_generation_is_published_once_and_is_frozen() -> None:
    signaled = Event()
    built: list[int] = []

    def build(generation: int) -> RefreshBatch:
        built.append(generation)
        return _batch(generation)

    coordinator = RefreshCoordinator(build, signaled.set)

    assert coordinator.request() is RequestResult.STARTED
    assert signaled.wait(1)
    result = coordinator.consume_latest()

    assert result == _batch(1)
    assert built == [1]
    assert coordinator.consume_latest() is None
    with pytest.raises(FrozenInstanceError):
        result.generation = 2
    with pytest.raises(TypeError):
        result.cleanup_counts["empty"] = 1


def test_ready_generation_is_not_overwritten_and_old_signal_cannot_consume_next() -> (
    None
):
    signals: Queue[None] = Queue()
    second_started = Event()
    release_second = Event()
    built: list[int] = []

    def build(generation: int) -> RefreshBatch:
        built.append(generation)
        if generation == 2:
            second_started.set()
            assert release_second.wait(1)
        return _batch(generation)

    coordinator = RefreshCoordinator(build, lambda: signals.put(None))
    assert coordinator.request() is RequestResult.STARTED
    signals.get(timeout=1)

    # A ready generation is retained intact; another request records one
    # follow-up but cannot start it before generation 1 is consumed.
    assert coordinator.request() is RequestResult.COALESCED
    assert built == [1]
    assert coordinator.consume_latest() == _batch(1)

    assert second_started.wait(1)
    # Simulate a duplicate/stale pipe callback while generation 2 is paused.
    assert coordinator.consume_latest() is None

    release_second.set()
    signals.get(timeout=1)
    assert coordinator.consume_latest() == _batch(2)
    assert built == [1, 2]


def test_twenty_requests_coalesce_to_one_follow_up_generation() -> None:
    signals: Queue[None] = Queue()
    first_started = Event()
    release_first = Event()
    built: list[int] = []

    def build(generation: int) -> RefreshBatch:
        built.append(generation)
        if generation == 1:
            first_started.set()
            assert release_first.wait(1)
        return _marked_batch(generation)

    coordinator = RefreshCoordinator(build, lambda: signals.put(None))
    assert coordinator.request() is RequestResult.STARTED
    assert first_started.wait(1)

    results: Queue[RequestResult] = Queue()
    requests = [
        Thread(target=lambda: results.put(coordinator.request())) for _ in range(20)
    ]
    for thread in requests:
        thread.start()
    for thread in requests:
        thread.join()

    assert [results.get_nowait() for _ in requests] == [RequestResult.COALESCED] * 20
    release_first.set()
    signals.get(timeout=1)
    first = coordinator.consume_latest()
    assert isinstance(first, RefreshBatch)
    assert first.snapshot.sessions[0].sid == "gen-1"
    assert first.cleanup_plan.empty[0].sid == "gen-1"
    signals.get(timeout=1)
    latest = coordinator.consume_latest()
    assert isinstance(latest, RefreshBatch)
    assert latest.generation == 2
    assert latest.snapshot.sessions[0].sid == "gen-2"
    assert latest.cleanup_plan.empty[0].sid == "gen-2"
    assert built == [1, 2]


def test_batch_builder_reads_sources_once_and_derives_one_coherent_world() -> None:
    session = Session(
        sid="gen-7",
        cwd="/tmp/project",
        label="generation seven",
        mtime=7.0,
        prompts=1,
        pid=None,
        alive=False,
        current=False,
    )
    older = RCProject(
        name="older",
        directory="/tmp/older",
        trusted=True,
        in_list=True,
        status="stopped",
        auto_start=False,
    )
    active = RCProject(
        name="active",
        directory="/tmp/project",
        trusted=True,
        in_list=True,
        status="stopped",
        auto_start=False,
    )
    snapshot = WorldSnapshot(sessions=[session], rc_projects=[older, active])
    plan = CleanupPlan(short=[session], orphan_entries=["gone"])
    snapshot_calls = 0
    plan_calls = 0

    def read_snapshot() -> WorldSnapshot:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return snapshot

    def make_plan(
        sessions,
        session_procs,
        cur,
        agent_jobs,
        agents_map,
    ) -> CleanupPlan:
        nonlocal plan_calls
        plan_calls += 1
        assert sessions is snapshot.sessions
        assert session_procs is snapshot.session_procs
        assert cur is snapshot.cur
        assert agent_jobs is snapshot.agent_jobs
        assert agents_map is snapshot.agents_map
        return plan

    result = build_refresh_result(
        7,
        snapshot_builder=read_snapshot,
        cleanup_builder=make_plan,
    )

    assert isinstance(result, RefreshBatch)
    assert result.generation == 7
    assert result.snapshot is snapshot
    assert result.cleanup_plan is plan
    assert result.cleanup_counts == {
        "empty": 0,
        "short": 1,
        "orphan_dirs": 1,
        "zombie_procs": 0,
        "aged_entries": 0,
    }
    assert result.session_stats == {
        "total": 1,
        "empty": 0,
        "short": 1,
        "orphans": 1,
    }
    assert [project.name for project in result.ordered_projects] == [
        "active",
        "older",
    ]
    assert snapshot_calls == plan_calls == 1


def test_expected_source_error_is_an_explicit_failure() -> None:
    def fail() -> WorldSnapshot:
        raise PermissionError(13, "denied", "/tmp/runtime")

    result = build_refresh_result(4, snapshot_builder=fail)

    assert result == RefreshFailure(
        generation=4,
        source="/tmp/runtime",
        detail="[Errno 13] denied: '/tmp/runtime'",
    )
    with pytest.raises(FrozenInstanceError):
        result.detail = "hidden"


def test_programming_error_is_not_converted_to_refresh_failure() -> None:
    def fail() -> WorldSnapshot:
        raise ValueError("broken invariant")

    with pytest.raises(ValueError, match="broken invariant"):
        build_refresh_result(9, snapshot_builder=fail)


def test_each_generation_invokes_each_normal_source_once() -> None:
    signals: Queue[None] = Queue()
    snapshot_calls = 0
    cleanup_calls = 0

    def read_snapshot() -> WorldSnapshot:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return WorldSnapshot()

    def make_plan(*args) -> CleanupPlan:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return CleanupPlan()

    coordinator = RefreshCoordinator(
        lambda generation: build_refresh_result(
            generation,
            snapshot_builder=read_snapshot,
            cleanup_builder=make_plan,
        ),
        lambda: signals.put(None),
    )

    for generation in (1, 2):
        assert coordinator.request() is RequestResult.STARTED
        signals.get(timeout=1)
        result = coordinator.consume_latest()
        assert isinstance(result, RefreshBatch)
        assert result.generation == generation

    assert snapshot_calls == cleanup_calls == 2


def test_close_rejects_requests_and_drops_late_worker_completion() -> None:
    signals: Queue[None] = Queue()
    started = Event()
    release = Event()
    finished = Event()

    def build(generation: int) -> RefreshBatch:
        started.set()
        assert release.wait(1)
        finished.set()
        return _batch(generation)

    coordinator = RefreshCoordinator(build, lambda: signals.put(None))
    assert coordinator.request() is RequestResult.STARTED
    assert started.wait(1)

    assert coordinator.close() is True
    assert coordinator.close() is False
    assert coordinator.request() is RequestResult.CLOSED
    assert coordinator.consume_latest() is None

    release.set()
    assert finished.wait(1)
    with pytest.raises(Empty):
        signals.get_nowait()
    assert coordinator.consume_latest() is None


def test_programming_error_stays_observable_and_does_not_wedge_requests(
    monkeypatch,
) -> None:
    errors: Queue[ExceptHookArgs] = Queue()
    signals: Queue[None] = Queue()
    attempts = 0

    def build(generation: int) -> RefreshBatch:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("broken invariant")
        return _batch(generation)

    monkeypatch.setattr(threading, "excepthook", errors.put)
    coordinator = RefreshCoordinator(build, lambda: signals.put(None))

    assert coordinator.request() is RequestResult.STARTED
    error = errors.get(timeout=1)
    assert error.exc_type is ValueError
    assert str(error.exc_value) == "broken invariant"

    assert coordinator.request() is RequestResult.STARTED
    signals.get(timeout=1)
    assert coordinator.consume_latest() == _batch(2)
