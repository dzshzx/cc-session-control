"""Fresh-evidence ownership at public destructive cleanup seams."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from cc_session_control import cli_commands
from cc_session_control.actions import agent_ops
from cc_session_control.config import cfg
from cc_session_control.data import (
    cleanup,
    cleanup_liveness,
    liveness,
    proc,
    sessions,
    transcripts,
)
from cc_session_control.data.removal import CleanupExecution, CleanupPlan
from cc_session_control.models import AgentJob, Session, SessionProc
from cc_session_control.views import _sessions_cleanup as cleanup_view


def _session(path, sid: str = "target-sid") -> Session:
    return Session(
        sid=sid,
        cwd="/tmp/project",
        label="target",
        mtime=0.0,
        prompts=0,
        pid=None,
        alive=False,
        current=False,
        file=str(path),
    )


def _stale_kwargs(function, **values):
    """Supply legacy evidence only while the public seam still exposes it."""
    parameters = inspect.signature(function).parameters
    return {name: value for name, value in values.items() if name in parameters}


def _complete_ancestors(monkeypatch) -> None:
    monkeypatch.setattr(
        proc,
        "probe_current_ancestors",
        lambda: proc.AncestorProbe(frozenset()),
    )


def _bomb_removal(monkeypatch) -> None:
    monkeypatch.setattr(
        cleanup,
        "remove_anchored",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not delete")),
    )


@pytest.mark.parametrize(
    ("executor", "forbidden"),
    [
        (cleanup.execute_orphan_removals, {"sessions", "known"}),
        (cleanup.execute_zombie_removals, {"session_procs", "cur"}),
        (
            cleanup.execute_session_removals,
            {"session_procs", "agents_map", "cur"},
        ),
    ],
)
def test_public_destructive_executors_do_not_accept_caller_liveness(
    executor,
    forbidden,
):
    assert forbidden.isdisjoint(inspect.signature(executor).parameters)


def test_orphan_executor_ignores_stale_known_set_and_keeps_fresh_known_sid(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _complete_ancestors(monkeypatch)
    _bomb_removal(monkeypatch)
    sid = "fresh-registry-sid"
    evidence = liveness.LivenessSnapshot(
        session_procs=(SessionProc(pid=41, sid=sid, proc_alive=False),),
    )
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: evidence,
    )
    monkeypatch.setattr(
        cleanup.transcripts,
        "load_inventory",
        lambda _root: transcripts.TranscriptInventory(),
    )

    result = cleanup.execute_orphan_removals(
        [f"session-env/{sid}"],
        **_stale_kwargs(
            cleanup.execute_orphan_removals,
            sessions=[],
            known=set(),
        ),
    )

    assert [notice.target for notice in result.skipped] == [f"session-env/{sid}"]


def test_orphan_executor_keeps_transcript_created_after_preview(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _complete_ancestors(monkeypatch)
    _bomb_removal(monkeypatch)
    sid = "new-transcript-sid"
    evidence = liveness.LivenessSnapshot()
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: evidence,
    )
    monkeypatch.setattr(
        cleanup.transcripts,
        "load_inventory",
        lambda _root: transcripts.TranscriptInventory(
            (
                transcripts.TranscriptRecord(
                    sid=sid,
                    cwd="/tmp/project",
                    path=str(tmp_path / f"{sid}.jsonl"),
                    mtime=0.0,
                ),
            )
        ),
    )

    result = cleanup.execute_orphan_removals(
        [f"uploads/{sid}"],
        **_stale_kwargs(
            cleanup.execute_orphan_removals,
            sessions=[],
        ),
    )

    assert [notice.target for notice in result.skipped] == [f"uploads/{sid}"]


def test_orphan_executor_refuses_all_targets_when_transcripts_are_incomplete(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _complete_ancestors(monkeypatch)
    targets = [
        "session-env/unreadable-sid",
        "uploads/another-unreadable-sid",
    ]
    for entry in targets:
        label, _, sid = entry.partition("/")
        (tmp_path / label / sid).mkdir(parents=True)
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    issue = transcripts.TranscriptIssue(
        "session transcript",
        "/runtime/projects/project/unreadable-sid.jsonl",
        "permission denied",
    )
    monkeypatch.setattr(
        cleanup.transcripts,
        "load_inventory",
        lambda _root: transcripts.TranscriptInventory(issues=(issue,)),
    )
    _bomb_removal(monkeypatch)

    result = cleanup.execute_orphan_removals(targets)

    assert [notice.target for notice in result.refused] == targets
    assert all(
        notice.reason == "transcript evidence incomplete; nothing deleted"
        for notice in result.refused
    )
    assert result.issues == [
        cleanup.CleanupIssue(
            "session transcript",
            "permission denied",
            "/runtime/projects/project/unreadable-sid.jsonl",
        )
    ]
    assert all((tmp_path / entry).is_dir() for entry in targets)


def test_orphan_executor_refuses_deletion_on_malformed_transcript_json(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    target = "uploads/malformed-transcript-sid"
    (tmp_path / target).mkdir(parents=True)
    transcript = tmp_path / "projects" / "project" / "malformed-transcript-sid.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"cwd":"/work/project"\n', encoding="utf-8")
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    _bomb_removal(monkeypatch)

    result = cleanup.execute_orphan_removals([target])

    assert [notice.target for notice in result.refused] == [target]
    assert result.issues[0].source == "session transcript"
    assert result.issues[0].path == str(transcript)
    assert "invalid JSON" in result.issues[0].error
    assert (tmp_path / target).is_dir()


def test_zombie_executor_ignores_stale_dead_row_and_keeps_fresh_live_pid(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _complete_ancestors(monkeypatch)
    _bomb_removal(monkeypatch)
    fresh = SessionProc(pid=77, sid="revived", proc_alive=True)
    stale = SessionProc(pid=77, sid="revived", proc_alive=False)
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(session_procs=(fresh,)),
    )

    result = cleanup.execute_zombie_removals(
        [77],
        **_stale_kwargs(
            cleanup.execute_zombie_removals,
            session_procs=[stale],
            cur=set(),
        ),
    )

    assert [notice.target for notice in result.skipped] == ["77"]


def test_session_executor_ignores_stale_rows_and_keeps_fresh_current_sid(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _complete_ancestors(monkeypatch)
    _bomb_removal(monkeypatch)
    target = _session(tmp_path / "target.jsonl")
    current = SessionProc(pid=88, sid=target.sid, proc_alive=False)
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(
            session_procs=(current,),
            cur=frozenset({88}),
        ),
    )

    result = cleanup.execute_session_removals(
        [target],
        **_stale_kwargs(
            cleanup.execute_session_removals,
            session_procs=[],
            agents_map={},
            cur=set(),
        ),
    )

    assert [notice.target for notice in result.skipped] == [target.sid]


def test_public_agent_artifact_removal_revalidates_live_sid(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _bomb_removal(monkeypatch)
    sid = "live-agent-sid"
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(agents_map={sid: 99}),
    )

    result = cleanup.remove_agent_artifacts("live-agt", sid)

    assert [notice.target for notice in result.skipped] == ["live-agt"]


@pytest.mark.parametrize("kind", ["orphan", "zombie", "session"])
def test_public_executors_refuse_incomplete_typed_liveness(
    kind,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _complete_ancestors(monkeypatch)
    _bomb_removal(monkeypatch)
    issue = liveness.LivenessIssue("process stat", "/proc/9/stat", "unreadable")
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(issues=(issue,)),
    )
    monkeypatch.setattr(
        cleanup.transcripts,
        "load_inventory",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("incomplete evidence must refuse before transcript scan")
        ),
    )
    if kind == "orphan":
        result = cleanup.execute_orphan_removals(["session-env/sid"])
    elif kind == "zombie":
        result = cleanup.execute_zombie_removals([9])
    else:
        result = cleanup.execute_session_removals(
            [_session(tmp_path / "sid.jsonl", "sid")]
        )

    assert [notice.reason for notice in result.refused] == [
        "liveness evidence incomplete; nothing deleted"
    ]
    assert result.issues[0].source == "process stat"


def test_aged_executor_does_not_acquire_unrelated_liveness(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        cleanup_liveness.liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(
            AssertionError("age-only cleanup must not read liveness")
        ),
    )

    result = cleanup.execute_aged_removals([])

    assert not result.incomplete


def test_cli_orphan_apply_does_not_inject_transcript_evidence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    _complete_ancestors(monkeypatch)
    orphan = cfg.session_env_dir / "ghost"
    orphan.mkdir(parents=True)
    monkeypatch.setattr(
        liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    scan_calls: list[object] = []
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda inputs=None: scan_calls.append(inputs) or sessions.SessionScanResult(),
    )
    called: dict[str, object] = {}

    def execute(entries, **kwargs):
        called.update(entries=entries, kwargs=kwargs)
        return CleanupExecution(completed=list(entries))

    monkeypatch.setattr(cleanup, "execute_orphan_removals", execute)
    args = SimpleNamespace(
        max_prompts=0,
        apply=True,
        sweep_orphans=True,
        sweep_zombies=False,
        sweep_aged=False,
    )

    assert cli_commands.handle_prune(args) == 0
    assert len(scan_calls) == 1
    assert called["entries"] == ["session-env/ghost"]
    assert set(called["kwargs"]) == {"anchors"}


def test_tui_orphan_adapter_does_not_inject_transcript_evidence(monkeypatch):
    monkeypatch.setattr(
        cleanup_view,
        "scan",
        lambda: (_ for _ in ()).throw(
            AssertionError("TUI must leave fresh transcript ownership to data")
        ),
        raising=False,
    )
    called: dict[str, object] = {}

    def execute(entries, **kwargs):
        called.update(entries=entries, kwargs=kwargs)
        return CleanupExecution(completed=list(entries))

    monkeypatch.setattr(cleanup_view, "execute_orphan_removals", execute)
    plan = CleanupPlan(orphan_entries=("session-env/ghost",))

    result = cleanup_view._execute_orphans(plan, ["session-env/ghost"])

    assert result.completed == ["session-env/ghost"]
    assert called["entries"] == ["session-env/ghost"]
    assert set(called["kwargs"]) == {"anchors"}


def test_remove_job_routes_final_revalidation_to_public_cleanup(
    monkeypatch,
):
    job = AgentJob(
        short="agent-id",
        sid="session-sid",
        resume_sid="session-sid",
    )
    expected = CleanupExecution(completed=[job.short])
    called: dict[str, object] = {}

    def remove(short, sid, **kwargs):
        called.update(short=short, sid=sid, kwargs=kwargs)
        return expected

    monkeypatch.setattr(cleanup, "remove_agent_artifacts", remove)
    monkeypatch.setattr(
        agent_ops.liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(
            AssertionError("caller must not own final liveness evidence")
        ),
    )

    assert agent_ops.remove_job(job) is expected
    assert called["short"] == job.short
    assert called["sid"] == job.sid
    assert set(called["kwargs"]) == {"anchors"}
