"""Atomic refresh generations at the coordinator's public seam."""

import threading
from dataclasses import FrozenInstanceError
from queue import Empty, Queue
from threading import Event, ExceptHookArgs, Thread

import pytest

from cc_session_control.config import cfg
from cc_session_control.data import cleanup
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.environment_ledger import (
    LedgerRead,
    LedgerReadState,
    LedgerUpdate,
    LedgerUpdateState,
)
from cc_session_control.data.environments import Reconciliation
from cc_session_control.data.liveness import LivenessIssue, LivenessSnapshot
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from cc_session_control.data.refresh import (
    RefreshBatch,
    RefreshCoordinator,
    RefreshFailure,
    RequestResult,
    build_refresh_result,
)
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.models import (
    AgentJob,
    BridgeEnv,
    EnvRecord,
    RCProject,
    RCServer,
    Session,
    SessionProc,
)


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


def test_refresh_batch_copies_and_freezes_nested_handoff_containers() -> None:
    session = Session(
        sid="session-1",
        cwd="/tmp/project",
        label="published",
        mtime=1.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
    )
    sessions = [session]
    agent_jobs = []
    projects = []
    servers = []
    observed = []
    referenced = []
    session_procs = []
    agents_map = {"session-1": None}
    current_pids = {123}
    empty = [session]
    orphan_entries = ["projects/orphan"]

    batch = RefreshBatch(
        generation=1,
        snapshot=WorldSnapshot(
            sessions=sessions,
            agent_jobs=agent_jobs,
            rc_projects=projects,
            rc_servers=servers,
            observed_envs=observed,
            file_referenced_envs=referenced,
            session_procs=session_procs,
            agents_map=agents_map,
            cur=current_pids,
        ),
        cleanup_plan=CleanupPlan(
            empty=empty,
            orphan_entries=orphan_entries,
        ),
        cleanup_counts={},
        session_stats={},
        ordered_projects=(),
    )

    sessions.append(session)
    agent_jobs.append(object())
    projects.append(object())
    servers.append(object())
    observed.append(object())
    referenced.append(object())
    session_procs.append(object())
    agents_map["later"] = 7
    current_pids.add(456)
    empty.append(session)
    orphan_entries.append("projects/later")

    assert batch.snapshot.sessions == (session,)
    assert batch.snapshot.agent_jobs == ()
    assert batch.snapshot.rc_projects == ()
    assert batch.snapshot.rc_servers == ()
    assert batch.snapshot.observed_envs == ()
    assert batch.snapshot.file_referenced_envs == ()
    assert batch.snapshot.session_procs == ()
    assert dict(batch.snapshot.agents_map) == {"session-1": None}
    assert batch.snapshot.cur == frozenset({123})
    assert batch.cleanup_plan.empty == (session,)
    assert batch.cleanup_plan.orphan_entries == ("projects/orphan",)
    with pytest.raises(AttributeError):
        batch.snapshot.sessions.append(session)
    with pytest.raises(TypeError):
        batch.snapshot.agents_map["later"] = 7
    with pytest.raises(AttributeError):
        batch.snapshot.cur.add(456)
    with pytest.raises(AttributeError):
        batch.cleanup_plan.empty.append(session)


def test_refresh_batch_deeply_freezes_reachable_models_and_results() -> None:
    hidden = {"tool"}
    respawn_flags = ["--verbose"]
    project_document = {
        "/tmp/project": {
            "hasTrustDialogAccepted": True,
            "metadata": {"tags": ["one"]},
        }
    }
    session = Session(
        sid="session-1",
        cwd="/tmp/project",
        label="published",
        mtime=1.0,
        prompts=0,
        pid=123,
        alive=True,
        current=False,
        hidden=hidden,
    )
    session_proc = SessionProc(123, "session-1", proc_alive=True)
    job = AgentJob(
        "job-1",
        "session-1",
        "session-1",
        respawn_flags=respawn_flags,
        host_pid=123,
        host_alive=True,
    )
    project = RCProject(
        "project",
        "/tmp/project",
        True,
        True,
        "running",
        True,
    )
    server = RCServer("project", "/tmp/project", True, 456, "env-server", "running")
    environment = BridgeEnv("session", "bridge", "session-1", 1.0, 2.0, "current")
    ledger_entries = {("session", "bridge"): environment}
    ledger_read = LedgerRead(LedgerReadState.READY, ledger_entries)
    ledger_update = LedgerUpdate(
        LedgerUpdateState.UNCHANGED,
        ledger_entries,
        ledger_read,
    )
    observed = EnvRecord("session", "bridge", "session-1")
    current_environments = [environment]
    reconciliation = Reconciliation(
        current=current_environments,
        observed=[observed],
        file_referenced=[observed],
        ledger=ledger_update,
    )
    settings = ProjectSettingsResult(
        ProjectSettingsState.AVAILABLE,
        project_document,
    )
    batch = RefreshBatch(
        generation=1,
        snapshot=WorldSnapshot(
            sessions=[session],
            agent_jobs=[job],
            rc_projects=[project],
            rc_project_settings=settings,
            rc_servers=[server],
            observed_envs=[observed],
            file_referenced_envs=[observed],
            environment_reconciliation=reconciliation,
            session_procs=[session_proc],
            liveness_snapshot=LivenessSnapshot(
                session_procs=[session_proc],
                agent_jobs=[job],
            ),
        ),
        cleanup_plan=CleanupPlan(empty=[session]),
        cleanup_counts={"empty": 1},
        session_stats={"total": 1},
        ordered_projects=[project],
    )

    hidden.add("later")
    respawn_flags.append("--later")
    project_document["/tmp/project"]["hasTrustDialogAccepted"] = False
    project_document["/tmp/project"]["metadata"]["tags"].append("later")
    ledger_entries[("session", "other")] = BridgeEnv("session", "other")
    current_environments.append(BridgeEnv("session", "later"))

    assert batch.snapshot.sessions[0].hidden == frozenset({"tool"})
    assert batch.snapshot.agent_jobs[0].respawn_flags == ("--verbose",)
    published_settings = batch.snapshot.rc_project_settings.projects["/tmp/project"]
    assert published_settings["hasTrustDialogAccepted"] is True
    assert published_settings["metadata"]["tags"] == ("one",)
    assert tuple(reconciliation.ledger.entries) == (("session", "bridge"),)
    assert reconciliation.current == (environment,)
    with pytest.raises(FrozenInstanceError):
        batch.snapshot.sessions = ()
    with pytest.raises(FrozenInstanceError):
        batch.cleanup_plan.empty = ()
    with pytest.raises(FrozenInstanceError):
        batch.snapshot.sessions[0].label = "changed"
    with pytest.raises(AttributeError):
        batch.snapshot.sessions[0].hidden.add("changed")
    with pytest.raises(FrozenInstanceError):
        batch.snapshot.session_procs[0].status = "changed"
    with pytest.raises(FrozenInstanceError):
        batch.snapshot.agent_jobs[0].host_alive = False
    with pytest.raises(AttributeError):
        batch.snapshot.agent_jobs[0].respawn_flags.append("--changed")
    with pytest.raises(FrozenInstanceError):
        batch.snapshot.rc_projects[0].status = "stopped"
    with pytest.raises(FrozenInstanceError):
        batch.snapshot.rc_servers[0].status = "stopped"
    with pytest.raises(FrozenInstanceError):
        batch.snapshot.environment_reconciliation.current[0].status = "orphan"
    with pytest.raises(AttributeError):
        batch.snapshot.environment_reconciliation.current.append(environment)
    with pytest.raises(TypeError):
        batch.snapshot.environment_reconciliation.ledger.entries[
            ("session", "other")
        ] = environment
    with pytest.raises(TypeError):
        ledger_read.entries[("session", "other")] = environment
    with pytest.raises(TypeError):
        batch.cleanup_plan.orphan_anchors["projects/later"] = None
    with pytest.raises(TypeError):
        batch.snapshot.rc_project_settings.projects["/tmp/other"] = {}
    with pytest.raises(TypeError):
        published_settings["hasTrustDialogAccepted"] = False
    with pytest.raises(TypeError):
        published_settings["metadata"]["tags"][0] = "changed"


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
    evidence = LivenessSnapshot()
    snapshot = WorldSnapshot(
        sessions=[session],
        rc_projects=[older, active],
        liveness_snapshot=evidence,
    )
    plan = CleanupPlan(short=[session], orphan_entries=["gone"])
    snapshot_calls = 0
    plan_calls = 0

    def read_snapshot() -> WorldSnapshot:
        nonlocal snapshot_calls
        snapshot_calls += 1
        return snapshot

    def make_plan(sessions, generation_evidence) -> CleanupPlan:
        nonlocal plan_calls
        plan_calls += 1
        assert sessions is snapshot.sessions
        assert generation_evidence is evidence
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


def test_refresh_plan_uses_generation_evidence_without_second_probe(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    session = Session(
        sid="generation-session",
        cwd="/tmp/project",
        label="generation session",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=str(tmp_path / "projects" / "generation-session.jsonl"),
    )
    evidence = LivenessSnapshot()
    snapshot = WorldSnapshot(
        sessions=(session,),
        liveness_snapshot=evidence,
    )

    def unexpected_acquisition(*_args, **_kwargs):
        raise AssertionError("refresh planning must not reacquire liveness")

    monkeypatch.setattr(cleanup.proc, "probe_current_ancestors", unexpected_acquisition)
    monkeypatch.setattr(cleanup, "fill_liveness_inputs", unexpected_acquisition)

    result = build_refresh_result(8, snapshot_builder=lambda: snapshot)

    assert isinstance(result, RefreshBatch)
    assert [candidate.sid for candidate in result.cleanup_plan.empty] == [
        "generation-session"
    ]


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


def test_incomplete_liveness_is_failed_before_cleanup_plan_build() -> None:
    snapshot = WorldSnapshot(
        liveness_snapshot=LivenessSnapshot(
            issues=(
                LivenessIssue(
                    "session registry",
                    "/runtime/sessions/broken.json",
                    "invalid JSON",
                ),
                LivenessIssue(
                    "claude agents --json",
                    None,
                    "exit status 7",
                ),
            )
        )
    )
    cleanup_calls = 0

    def make_plan(*args) -> CleanupPlan:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return CleanupPlan()

    result = build_refresh_result(
        8,
        snapshot_builder=lambda: snapshot,
        cleanup_builder=make_plan,
    )

    assert isinstance(result, RefreshFailure)
    assert result.generation == 8
    assert result.source == "session registry (/runtime/sessions/broken.json)"
    assert "invalid JSON" in result.detail
    assert "claude agents --json: exit status 7" in result.detail
    assert cleanup_calls == 0


def test_missing_liveness_evidence_is_failed_before_cleanup_plan_build() -> None:
    cleanup_calls = 0

    def make_plan(*_args) -> CleanupPlan:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return CleanupPlan()

    result = build_refresh_result(
        9,
        snapshot_builder=WorldSnapshot,
        cleanup_builder=make_plan,
    )

    assert result == RefreshFailure(
        9,
        "liveness snapshot",
        "missing generation liveness evidence",
    )
    assert cleanup_calls == 0


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
        return WorldSnapshot(liveness_snapshot=LivenessSnapshot())

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
