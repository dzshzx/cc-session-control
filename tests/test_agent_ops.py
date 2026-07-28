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


def test_respawn_launches_in_tmux_and_returns_cmd(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ao.tmux,
        "run_in_tmux",
        lambda session, window, cmd: (
            captured.update(session=session, window=window, cmd=cmd) or "proj:1"
        ),
    )
    job = _make_job(resume_sid="sid-xyz", respawn_flags=["--bg-extra"])
    out = ao.respawn(job)
    assert out == "claude --resume sid-xyz --bg-extra --bg"
    assert captured["cmd"] == out
    # per-project grouping: the job's cwd basename, not one shared session
    assert captured["session"] == "proj"


def test_respawn_result_retains_tmux_failure(monkeypatch):
    monkeypatch.setattr(ao.tmux, "run_in_tmux", lambda *_: None)

    result = ao.respawn_result(_make_job(resume_sid="sid-xyz"))

    assert result.command == "claude --resume sid-xyz --bg"
    assert result.target is None
    assert result.success is False


# --- remove_job: settled only, current-determinable only ---


def test_remove_job_refuses_live(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
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
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
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
    called = {"host": 0}
    monkeypatch.setattr(
        ao, "job_host", lambda job: called.__setitem__("host", 1) or (None, False)
    )
    result = ao.remove_job(_make_job())
    assert len(result.refused) == 1
    # Refused before even resolving the host pid.
    assert called["host"] == 0


def test_remove_job_refuses_real_agents_failure_before_deleting(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
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


# --- watch: read-only path lookup ---


def test_watch_returns_path_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    job = _make_job(short="abcdef01")
    job_dir = tmp_path / "jobs" / job.short
    job_dir.mkdir(parents=True)
    timeline = job_dir / "timeline.jsonl"
    timeline.write_text("{}\n")
    assert ao.watch(job) == str(timeline)


def test_watch_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    assert ao.watch(_make_job()) is None


# --- resume_takeover: routes through the existing resume path ---


def test_resume_takeover_builds_session_for_existing_resume_path(monkeypatch):
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
        ao,
        "job_host",
        lambda *_: (_ for _ in ()).throw(AssertionError("legacy join must not run")),
    )
    monkeypatch.setattr(
        ao.tmux,
        "find_session_window",
        lambda pids: "proj:4" if pids == [4242] else None,
    )
    job = _make_job(resume_sid="sid-take", cwd="/tmp/proj")

    s = ao.resume_takeover(job)
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

    # The adapter feeds the EXISTING resume machinery unchanged: a live,
    # non-current session is taken over (old pid killed first).
    assert (
        resume_cmd(s)
        == "kill 4242 && sleep 1 && cd /tmp/proj && claude --resume sid-take"
    )


def test_resume_takeover_dead_worker_no_kill(monkeypatch):
    monkeypatch.setattr(
        ao.liveness,
        "liveness_inputs",
        lambda: liveness.LivenessSnapshot(),
    )
    monkeypatch.setattr(
        ao,
        "job_host",
        lambda *_: (_ for _ in ()).throw(AssertionError("legacy join must not run")),
    )
    monkeypatch.setattr(
        ao.tmux,
        "find_session_window",
        lambda pids: (_ for _ in ()).throw(AssertionError("dead: no tmux lookup")),
    )
    job = _make_job(resume_sid="sid-dead", cwd="/tmp/proj")
    s = ao.resume_takeover(job)
    assert s.alive is False
    assert s.tmux_target is None
    assert resume_cmd(s) == "cd /tmp/proj && claude --resume sid-dead"


def test_resume_takeover_compatibility_refusal_returns_no_session(monkeypatch):
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

    with pytest.raises(RuntimeError, match="session registry"):
        ao.resume_takeover(_make_job())


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


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ao.AgentStopState.STOPPED, True),
        (ao.AgentStopState.NOT_RUNNING, False),
        (ao.AgentStopState.REFUSED, False),
        (ao.AgentStopState.FAILED, False),
    ],
)
def test_stop_job_bool_compatibility_derives_typed_result(
    state,
    expected,
    monkeypatch,
):
    monkeypatch.setattr(
        ao,
        "stop_job_result",
        lambda _job: ao.AgentStopResult(state),
    )

    assert ao.stop_job(_make_job()) is expected


# --- job_host: join sid -> sessions/<pid>.json ---


def test_job_host_prefers_live_match(monkeypatch):
    procs = [
        SessionProc(pid=100, sid="sid-a", proc_start="111"),
        SessionProc(pid=200, sid="sid-a", proc_start="222"),
        SessionProc(pid=300, sid="other", proc_start="333"),
    ]
    monkeypatch.setattr(ao.registry, "read_session_procs", lambda *a, **k: procs)
    monkeypatch.setattr(
        ao.proc,
        "probe_pid",
        lambda pid, start: ao.proc.PidProbe(pid, pid == 200 and start == "222"),
    )
    job = _make_job(sid="sid-a")
    assert ao.job_host(job) == (200, True)


def test_job_host_none_when_no_sessions_file(monkeypatch):
    monkeypatch.setattr(ao.registry, "read_session_procs", lambda *a, **k: [])
    monkeypatch.setattr(
        ao.proc,
        "probe_pid",
        lambda pid, start: ao.proc.PidProbe(pid, True),
    )
    assert ao.job_host(_make_job(sid="sid-missing")) == (None, False)


def test_job_host_dead_when_no_live_match(monkeypatch):
    procs = [SessionProc(pid=100, sid="sid-a", proc_start="111")]
    monkeypatch.setattr(ao.registry, "read_session_procs", lambda *a, **k: procs)
    monkeypatch.setattr(
        ao.proc,
        "probe_pid",
        lambda pid, start: ao.proc.PidProbe(pid, False),
    )
    assert ao.job_host(_make_job(sid="sid-a")) == (100, False)


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
