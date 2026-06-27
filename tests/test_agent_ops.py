"""Background-agent lifecycle action tests (R4 / AC4)."""

import os

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
        ao.rc, "run_in_tmux",
        lambda session, window, cmd: captured.update(
            session=session, window=window, cmd=cmd) or True,
    )
    job = _make_job(resume_sid="sid-xyz", respawn_flags=["--bg-extra"])
    out = ao.respawn(job)
    assert out == "claude --resume sid-xyz --bg-extra --bg"
    assert captured["cmd"] == out
    assert captured["session"] == ao.cfg.tmux_session


# --- remove_job: settled only, current-determinable only ---

def test_remove_job_refuses_live(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (1234, True))
    removed_paths = []
    monkeypatch.setattr(ao.cleanup, "_remove_path",
                        lambda p: removed_paths.append(p) or True)
    assert ao.remove_job(_make_job()) is False
    assert removed_paths == []


def test_remove_job_deletes_settled(tmp_path, monkeypatch):
    monkeypatch.setattr(ao.cfg, "claude_home", tmp_path)
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (None, False))

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

    assert ao.remove_job(job) is True
    assert not job_dir.exists()
    for sub in ("session-env", "file-history", "tasks", "uploads"):
        assert not (tmp_path / sub / job.sid).exists()


def test_remove_job_refuses_without_proc(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: False)
    called = {"host": 0}
    monkeypatch.setattr(ao, "job_host",
                        lambda job: called.__setitem__("host", 1) or (None, False))
    assert ao.remove_job(_make_job()) is False
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
    job = _make_job(resume_sid="sid-take", cwd="/tmp/proj")

    s = ao.resume_takeover(job)
    assert s.sid == "sid-take"
    assert s.cwd == "/tmp/proj"
    assert s.pid == 4242
    assert s.alive is True
    assert s.current is False
    assert s.source == "bg"
    assert s.agent_short == job.short

    # The adapter feeds the EXISTING resume machinery unchanged: a live,
    # non-current session is taken over (old pid killed first).
    assert resume_cmd(s) == "kill 4242 && sleep 1 && cd /tmp/proj && claude --resume sid-take"


def test_resume_takeover_dead_worker_no_kill(monkeypatch):
    monkeypatch.setattr(ao, "job_host", lambda job: (None, False))
    monkeypatch.setattr(ao.proc, "ancestor_pids", lambda: set())
    job = _make_job(resume_sid="sid-dead", cwd="/tmp/proj")
    s = ao.resume_takeover(job)
    assert s.alive is False
    assert resume_cmd(s) == "cd /tmp/proj && claude --resume sid-dead"


# --- stop_job: only a confirmed-live joined host pid ---

def test_stop_job_noop_when_no_host_pid(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (None, False))
    kills = {"n": 0}
    monkeypatch.setattr(ao.os, "kill", lambda *_: kills.__setitem__("n", kills["n"] + 1))
    assert ao.stop_job(_make_job()) is False
    assert kills["n"] == 0


def test_stop_job_noop_when_host_dead(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (1234, False))
    kills = {"n": 0}
    monkeypatch.setattr(ao.os, "kill", lambda *_: kills.__setitem__("n", kills["n"] + 1))
    assert ao.stop_job(_make_job()) is False
    assert kills["n"] == 0


def test_stop_job_kills_live_host(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(ao, "job_host", lambda job: (4242, True))
    calls = {"kill": None, "invalidate": 0}
    monkeypatch.setattr(ao.os, "kill", lambda pid, sig: calls.__setitem__("kill", (pid, sig)))
    monkeypatch.setattr(ao.time, "sleep", lambda *_: None)
    monkeypatch.setattr(ao.liveness, "invalidate_cache",
                        lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))
    assert ao.stop_job(_make_job()) is True
    assert calls["kill"] == (4242, ao.signal.SIGTERM)
    assert calls["invalidate"] == 1


def test_stop_job_refuses_without_proc(monkeypatch):
    monkeypatch.setattr(ao.proc, "current_determinable", lambda: False)
    monkeypatch.setattr(ao, "job_host", lambda job: (4242, True))
    kills = {"n": 0}
    monkeypatch.setattr(ao.os, "kill", lambda *_: kills.__setitem__("n", kills["n"] + 1))
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
    monkeypatch.setattr(ao.proc, "pid_alive",
                        lambda pid, start: pid == 200 and start == "222")
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


# --- AC4: help/keyhints carry orphan-risk warning + "接管" label ---

def test_keyhints_contains_takeover_label():
    assert "接管" in ao.KEYHINTS
    assert ao.TAKEOVER_LABEL == "接管"


def test_help_contains_orphan_risk_warning():
    assert "孤儿" in ao.HELP
    assert "接管" in ao.HELP
