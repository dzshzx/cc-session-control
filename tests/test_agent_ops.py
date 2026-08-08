"""Background-agent lifecycle action tests (R4 / AC4)."""

import json
import subprocess

import pytest

import cc_session_control.actions.agent_ops as ao
from cc_session_control.actions.session_ops import resume_cmd
from cc_session_control.data import liveness, registry
from cc_session_control.models import AgentJob, SessionProc


def _make_job(**overrides):
    defaults = dict(
        short="abcdef01",
        sid="abcdef0123456789",
        resume_sid="abcdef0123456789",
        state="idle",
        tempo="",
        cwd="/tmp/proj",
        name="worker",
        env_suffix="",
        respawn_flags=[],
        host_pid=None,
        host_alive=False,
    )
    defaults.update(overrides)
    return AgentJob(**defaults)


# --- respawn: exact command + spawns in tmux (not exec) ---


def test_respawn_cmd_exact_shlex():
    job = _make_job(resume_sid="sid-xyz", respawn_flags=["--model", "opus"])
    assert ao.respawn_cmd(job) == "claude --resume sid-xyz --model opus --bg"


def test_respawn_cmd_no_flags():
    job = _make_job(resume_sid="sid-xyz", respawn_flags=[])
    assert ao.respawn_cmd(job) == "claude --resume sid-xyz --bg"


def test_respawn_launches_from_job_project_in_shared_tmux(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ao.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: (
            captured.update(session=session, window=window, cmd=cmd)
            or ao.tmux.TmuxWriteResult(
                ao.tmux.TmuxWriteStage.NEW_WINDOW,
                ao.tmux.TmuxWriteState.SUCCEEDED,
                target="csctl:1",
            )
        ),
    )
    job = _make_job(
        cwd="/tmp/proj with space",
        resume_sid="sid-xyz",
        respawn_flags=["--bg-extra"],
    )
    out = ao.respawn_result(job).command
    assert out == "claude --resume sid-xyz --bg-extra --bg"
    assert captured["cmd"] == (
        "cd '/tmp/proj with space' && claude --resume sid-xyz --bg-extra --bg"
    )
    assert captured["session"] == "csctl"
    assert captured["window"] == "proj with space/worker-abcdef01"


def test_respawn_result_retains_tmux_failure(monkeypatch):
    monkeypatch.setattr(
        ao.tmux,
        "run_in_tmux_result",
        lambda *_: ao.tmux.TmuxWriteResult(
            ao.tmux.TmuxWriteStage.NEW_WINDOW,
            ao.tmux.TmuxWriteState.FAILED,
            detail="tmux unavailable",
        ),
    )

    result = ao.respawn_result(_make_job(resume_sid="sid-xyz"))

    assert result.command == "claude --resume sid-xyz --bg"
    assert result.target is None
    assert result.detail == "new-window: tmux unavailable"
    assert result.success is False


# --- remove_job: settled only, current-determinable only ---


def test_remove_job_refuses_live(monkeypatch):
    monkeypatch.setattr(
        ao.proc,
        "probe_current_ancestors",
        lambda: ao.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(
            agents_map={"abcdef0123456789": 1234},
        ),
    )
    removed_paths = []
    monkeypatch.setattr(
        ao.cleanup, "remove_anchored", lambda p: removed_paths.append(p) or True
    )
    result = ao.remove_job(_make_job())
    assert len(result.skipped) == 1
    assert removed_paths == []


def test_remove_job_deletes_settled(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        ao.proc,
        "probe_current_ancestors",
        lambda: ao.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )

    job = _make_job(short="abcdef01", sid="abcdef0123456789")

    # jobs/<short>/ with a state file.
    job_dir = tmp_path / "jobs" / job.short
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text("{}")

    # sid-keyed artifact dirs.
    for sub in ("session-env", "file-history", "tasks", "uploads"):
        d = tmp_path / sub / job.sid
        d.mkdir(parents=True)
        (d / "x").write_text("data")

    result = ao.remove_job(job)
    assert result.completed == [job.short]
    assert not job_dir.exists()
    for sub in ("session-env", "file-history", "tasks", "uploads"):
        assert not (tmp_path / sub / job.sid).exists()


def test_remove_job_refuses_without_proc(monkeypatch):
    issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(issues=(issue,)),
    )
    result = ao.remove_job(_make_job())
    assert len(result.refused) == 1


def test_remove_job_refuses_real_agents_failure_before_deleting(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        ao.proc,
        "probe_current_ancestors",
        lambda: ao.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(ao.proc, "ancestor_pids", lambda: set())
    job = _make_job()
    state = tmp_path / "jobs" / job.short / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"sessionId": job.sid}))
    completed = subprocess.CompletedProcess(
        [],
        5,
        stdout='[{"sessionId":"abcdef0123456789","pid":55}]',
        stderr="agents unavailable",
    )
    monkeypatch.setattr(ao.liveness.subprocess, "run", lambda *a, **k: completed)
    monkeypatch.setattr(
        ao.cleanup,
        "remove_anchored",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not delete")),
    )
    registry.invalidate_cache()
    liveness.invalidate_cache()

    result = ao.remove_job(job)

    assert len(result.refused) == 1
    assert result.issues[0].source == "claude agents --json"
    assert "exit status 5" in result.issues[0].error
    assert state.exists()


# --- watch: bounded read-only timeline data ---


def test_watch_streams_only_last_200_lines_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    job = _make_job(short="abcdef01")
    lines = [f"line-{index}\n" for index in range(205)]

    class TimelineStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return iter(lines)

        def read(self):
            raise AssertionError("timeline must not be read into memory at once")

    monkeypatch.setattr(
        ao, "open", lambda *_args, **_kwargs: TimelineStream(), raising=False
    )

    result = ao.watch(job)

    assert result.state is ao.TimelineReadState.READY
    assert result.lines == tuple(f"line-{index}" for index in range(5, 205))
    assert result.detail == ""


def test_watch_reports_typed_missing_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    result = ao.watch(_make_job())
    assert result.state is ao.TimelineReadState.MISSING
    assert result.lines == ()


def test_watch_reports_typed_read_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    monkeypatch.setattr(
        ao,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
        raising=False,
    )

    result = ao.watch(_make_job())

    assert result.state is ao.TimelineReadState.FAILED
    assert result.lines == ()
    assert result.detail == "denied"


# --- prepare_takeover: routes through the existing resume path ---


def test_prepare_takeover_builds_session_for_existing_resume_path(monkeypatch):
    calls = {"snapshot": 0}
    evidence = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=4242,
                sid="abcdef0123456789",
                proc_start="777",
                proc_alive=True,
            ),
        ),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    monkeypatch.setattr(ao.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(
        ao.tmux,
        "find_session_window_result",
        lambda pids: ao.tmux.SessionWindowResult("proj:4" if pids == [4242] else None),
    )
    job = _make_job(
        resume_sid="sid-take",
        cwd="/tmp/proj",
        host_pid=4242,
        host_alive=True,
    )

    result = ao.prepare_takeover(job)
    assert result.state is ao.TakeoverPreparationState.READY
    s = result.session
    assert s is not None
    assert s.sid == "sid-take"
    assert s.cwd == "/tmp/proj"
    assert s.pid == 4242
    assert s.alive is True
    assert s.current is False
    assert s.proc_start == "777"  # feeds take_over's kill-time pid-reuse recheck
    assert s.source == "bg"
    assert s.agent_short == job.short
    assert s.tmux_target == "proj:4"  # resident worker -> tmux Enter attaches in place
    assert calls["snapshot"] == 1

    # A copied live command carries only the durable sid. Runtime pid/start/cwd
    # are reacquired when the operator executes the command.
    assert resume_cmd(s) == "csctl resume --take-over sid-take"


def test_prepare_takeover_dead_worker_no_kill(monkeypatch):
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: (_ for _ in ()).throw(
            AssertionError("published dead job must not acquire liveness")
        ),
    )
    monkeypatch.setattr(
        ao.tmux,
        "find_session_window_result",
        lambda pids: (_ for _ in ()).throw(AssertionError("dead: no tmux lookup")),
    )
    job = _make_job(resume_sid="sid-dead", cwd="/tmp/proj")
    result = ao.prepare_takeover(job)
    assert result.state is ao.TakeoverPreparationState.READY
    s = result.session
    assert s is not None
    assert s.alive is False
    assert s.tmux_target is None
    assert resume_cmd(s) == "cd /tmp/proj && claude --resume sid-dead"


def test_prepare_takeover_refuses_incomplete_tmux_inventory(monkeypatch):
    evidence = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=4242,
                sid="abcdef0123456789",
                proc_start="777",
                proc_alive=True,
            ),
        ),
    )
    issue = ao.tmux.ResidencyIssue(
        "tmux list-panes",
        None,
        "tmux timed out after 5 seconds",
    )
    monkeypatch.setattr(ao.liveness, "liveness_inputs", lambda: evidence)
    monkeypatch.setattr(
        ao.tmux,
        "find_session_window_result",
        lambda _pids: ao.tmux.SessionWindowResult(issues=(issue,)),
    )

    result = ao.prepare_takeover(_make_job(host_pid=4242, host_alive=True))

    assert result.state is ao.TakeoverPreparationState.REFUSED
    assert result.session is None
    assert "tmux list-panes" in result.detail
    assert "tmux timed out after 5 seconds" in result.detail


def test_prepare_takeover_refuses_incomplete_liveness_evidence(monkeypatch):
    issue = liveness.LivenessIssue(
        "session registry",
        "/broken/session.json",
        "invalid JSON",
    )
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(issues=(issue,)),
    )

    result = ao.prepare_takeover(_make_job(host_pid=4242, host_alive=True))

    assert result.state is ao.TakeoverPreparationState.REFUSED
    assert result.session is None
    assert "session registry" in result.detail


# --- stop_job: only a confirmed-live joined host pid ---


def test_stop_job_result_refuses_when_current_session_is_undeterminable(
    monkeypatch,
):
    issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(issues=(issue,)),
    )
    monkeypatch.setattr(
        ao.session_ops,
        "take_over_result",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    result = ao.stop_job_result(_make_job())

    assert result.state is ao.AgentStopState.REFUSED
    assert "process ancestors" in result.detail


@pytest.mark.parametrize("source", ["session registry", "claude agents --json"])
def test_stop_job_result_refuses_incomplete_liveness_evidence(
    source,
    monkeypatch,
):
    evidence = liveness.LivenessSnapshot(
        issues=(liveness.LivenessIssue(source, "/broken", "invalid"),),
    )
    monkeypatch.setattr(ao.liveness, "liveness_inputs", lambda: evidence)
    monkeypatch.setattr(
        ao.session_ops,
        "take_over_result",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    result = ao.stop_job_result(_make_job())

    assert result.state is ao.AgentStopState.REFUSED
    assert source in result.detail
    assert "/broken" in result.detail


@pytest.mark.parametrize(
    "session_procs",
    [
        (),
        (
            SessionProc(
                pid=4242,
                sid="abcdef0123456789",
                proc_start="777",
                proc_alive=False,
            ),
        ),
    ],
)
def test_stop_job_result_reports_missing_or_dead_host_not_running(
    session_procs,
    monkeypatch,
):
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(session_procs=session_procs),
    )
    monkeypatch.setattr(
        ao.session_ops,
        "take_over_result",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    result = ao.stop_job_result(_make_job())

    assert result.state is ao.AgentStopState.NOT_RUNNING


@pytest.mark.parametrize(
    ("take_over_result", "expected"),
    [
        ("killed", "STOPPED"),
        ("gone", "NOT_RUNNING"),
        ("refused", "REFUSED"),
        ("failed", "FAILED"),
    ],
)
def test_stop_job_result_maps_take_over_without_second_scan(
    take_over_result,
    expected,
    monkeypatch,
):
    calls = {"snapshot": 0, "take_over": []}
    evidence = liveness.LivenessSnapshot(
        session_procs=(
            SessionProc(
                pid=4242,
                sid="abcdef0123456789",
                proc_start="777",
                proc_alive=True,
            ),
        ),
    )

    def snapshot():
        calls["snapshot"] += 1
        return evidence

    monkeypatch.setattr(ao.liveness, "liveness_inputs", snapshot)
    monkeypatch.setattr(
        ao.liveness,
        "live_session_procs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("compatibility scan must not run")
        ),
    )
    outcome = ao.session_ops.TakeOverOutcome(
        ao.session_ops.TakeOverState(take_over_result),
    )
    monkeypatch.setattr(
        ao.session_ops,
        "take_over_result",
        lambda pid, proc_start: calls["take_over"].append((pid, proc_start)) or outcome,
    )

    result = ao.stop_job_result(_make_job())

    assert result.state is getattr(ao.AgentStopState, expected)
    assert result.pid == 4242
    assert calls == {"snapshot": 1, "take_over": [(4242, "777")]}


# --- AC4: help/keyhints carry orphan-risk warning + "接回" label ---
# (both are generated from the view's single-source KEY_TABLE now)


def test_keyhints_contains_takeover_label():
    from cc_session_control.views._keytable import footer_hints
    from cc_session_control.views.agents import AgentsView

    assert "接回" in footer_hints(AgentsView.KEY_TABLE)


def test_help_contains_orphan_risk_warning():
    from cc_session_control.views._keytable import help_lines
    from cc_session_control.views.agents import AgentsView

    blob = "\n".join(help_lines(AgentsView.KEY_TABLE, AgentsView.HELP_LAYOUT))
    assert "孤儿" in blob
    assert "接管" in blob
