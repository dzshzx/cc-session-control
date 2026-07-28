"""Background-agent lifecycle action tests (R4 / AC4)."""

import cc_session_control.actions.agent_ops as ao
from cc_session_control.actions.session_ops import resume_cmd
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
    monkeypatch.setattr(ao, "job_host", lambda job, **kwargs: (1234, True))
    removed_paths = []
    monkeypatch.setattr(
        ao.cleanup, "_remove_path", lambda p: removed_paths.append(p) or True
    )
    result = ao.remove_job(_make_job())
    assert len(result.skipped) == 1
    assert removed_paths == []


def test_remove_job_deletes_settled(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job, **kwargs: (None, False))

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
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: False)
    called = {"host": 0}
    monkeypatch.setattr(
        ao, "job_host", lambda job: called.__setitem__("host", 1) or (None, False)
    )
    result = ao.remove_job(_make_job())
    assert len(result.refused) == 1
    # Refused before even resolving the host pid.
    assert called["host"] == 0


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
    monkeypatch.setattr(ao, "job_host", lambda job: (4242, True))
    monkeypatch.setattr(ao.proc, "ancestor_pids", lambda: set())
    monkeypatch.setattr(
        ao.liveness,
        "live_session_procs",
        lambda *a, **k: [
            SessionProc(pid=4242, sid="sid-take", proc_start="777", proc_alive=True)
        ],
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

    # The adapter feeds the EXISTING resume machinery unchanged: a live,
    # non-current session is taken over (old pid killed first).
    assert (
        resume_cmd(s)
        == "kill 4242 && sleep 1 && cd /tmp/proj && claude --resume sid-take"
    )


def test_resume_takeover_dead_worker_no_kill(monkeypatch):
    monkeypatch.setattr(ao, "job_host", lambda job: (None, False))
    monkeypatch.setattr(ao.proc, "ancestor_pids", lambda: set())
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


# --- stop_job: only a confirmed-live joined host pid ---


def test_stop_job_noop_when_no_host_pid(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (None, False))
    kills = {"n": 0}
    monkeypatch.setattr(
        ao.os, "kill", lambda *_: kills.__setitem__("n", kills["n"] + 1)
    )
    assert ao.stop_job(_make_job()) is False
    assert kills["n"] == 0


def test_stop_job_noop_when_host_dead(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (1234, False))
    kills = {"n": 0}
    monkeypatch.setattr(
        ao.os, "kill", lambda *_: kills.__setitem__("n", kills["n"] + 1)
    )
    assert ao.stop_job(_make_job()) is False
    assert kills["n"] == 0


def test_stop_job_kills_live_host(monkeypatch):
    # The kill itself is session_ops.take_over — stub/observe it at that seam.
    import cc_session_control.actions.session_ops as so

    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (4242, True))
    monkeypatch.setattr(ao.liveness, "live_session_procs", lambda *a, **k: [])
    calls = {"kill": None, "invalidate": 0}
    monkeypatch.setattr(so.proc, "pid_alive", lambda pid, start: True)
    monkeypatch.setattr(
        so.os, "kill", lambda pid, sig: calls.__setitem__("kill", (pid, sig))
    )
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        so,
        "invalidate_cache",
        lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1),
    )
    assert ao.stop_job(_make_job()) is True
    assert calls["kill"] == (4242, so.signal.SIGTERM)
    assert calls["invalidate"] == 1


def test_stop_job_refuses_without_proc(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: False)
    monkeypatch.setattr(ao, "job_host", lambda job: (4242, True))
    kills = {"n": 0}
    monkeypatch.setattr(
        ao.os, "kill", lambda *_: kills.__setitem__("n", kills["n"] + 1)
    )
    assert ao.stop_job(_make_job()) is False
    assert kills["n"] == 0


# --- job_host: join sid -> sessions/<pid>.json ---


def test_job_host_prefers_live_match(monkeypatch):
    procs = [
        SessionProc(pid=100, sid="sid-a", proc_start="111"),
        SessionProc(pid=200, sid="sid-a", proc_start="222"),
        SessionProc(pid=300, sid="other", proc_start="333"),
    ]
    monkeypatch.setattr(ao.registry, "read_session_procs", lambda *a, **k: procs)
    monkeypatch.setattr(
        ao.proc, "pid_alive", lambda pid, start: pid == 200 and start == "222"
    )
    job = _make_job(sid="sid-a")
    assert ao.job_host(job) == (200, True)


def test_job_host_none_when_no_sessions_file(monkeypatch):
    monkeypatch.setattr(ao.registry, "read_session_procs", lambda *a, **k: [])
    monkeypatch.setattr(ao.proc, "pid_alive", lambda pid, start: True)
    assert ao.job_host(_make_job(sid="sid-missing")) == (None, False)


def test_job_host_dead_when_no_live_match(monkeypatch):
    procs = [SessionProc(pid=100, sid="sid-a", proc_start="111")]
    monkeypatch.setattr(ao.registry, "read_session_procs", lambda *a, **k: procs)
    monkeypatch.setattr(ao.proc, "pid_alive", lambda pid, start: False)
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
