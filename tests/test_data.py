"""Data-layer unit tests — pure functions, transcript parsing, rc toggles."""

import time

import json
import subprocess

from cc_session_control.actions.session_ops import resume_cmd
from cc_session_control.config import cfg
from cc_session_control.data import liveness, registry
from cc_session_control.data.cleanup import cleanup_stats, prune_sessions
from cc_session_control.data.sessions import _parse_transcript
from cc_session_control.models import LiveInfo, Session


def _make_session(**overrides):
    defaults = dict(
        sid="abc123", cwd="/tmp/proj", label="test", mtime=0.0,
        prompts=0, pid=None, alive=False, current=False,
        hidden=set(), file="/tmp/abc123.jsonl",
    )
    defaults.update(overrides)
    return Session(**defaults)


# --- D1: prune_sessions ---

def test_prune_sessions_excludes_alive():
    now = time.time()
    old = now - 700
    sessions = [
        _make_session(sid="dead", prompts=0, mtime=old, alive=False),
        _make_session(sid="alive", prompts=0, mtime=old, alive=True, pid=999),
    ]
    pruned = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert "dead" in pruned
    assert "alive" not in pruned


def test_prune_sessions_excludes_current():
    now = time.time()
    old = now - 700
    sessions = [
        _make_session(sid="normal", prompts=0, mtime=old, current=False),
        _make_session(sid="cur", prompts=0, mtime=old, current=True),
    ]
    pruned = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert "normal" in pruned
    assert "cur" not in pruned


def test_prune_sessions_excludes_recent():
    now = time.time()
    sessions = [
        _make_session(sid="old", prompts=0, mtime=now - 700),
        _make_session(sid="recent", prompts=0, mtime=now - 100),
    ]
    pruned = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert "old" in pruned
    assert "recent" not in pruned


def test_prune_sessions_threshold():
    now = time.time()
    old = now - 700
    sessions = [
        _make_session(sid="p0", prompts=0, mtime=old),
        _make_session(sid="p2", prompts=2, mtime=old),
        _make_session(sid="p3", prompts=3, mtime=old),
    ]
    empties = {s.sid for s in prune_sessions(sessions, max_prompts=0)}
    assert empties == {"p0"}
    shorts = {s.sid for s in prune_sessions(sessions, max_prompts=2)}
    assert shorts == {"p0", "p2"}


# --- D1: resume_cmd ---

def test_resume_cmd_dead():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=False)
    cmd = resume_cmd(s)
    assert cmd == "cd /tmp/proj && claude --resume sid1"


def test_resume_cmd_alive_non_current():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    cmd = resume_cmd(s)
    assert cmd == "kill 4242 && sleep 1 && cd /tmp/proj && claude --resume sid1"


def test_resume_cmd_fork():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=False)
    cmd = resume_cmd(s, fork=True)
    assert cmd == "cd /tmp/proj && claude --resume sid1 --fork-session"


def test_resume_cmd_fork_while_alive_drops_kill_prefix():
    # Unified semantics (decision A): fork is a copy and leaves the original
    # running, so forking a live non-current session must NOT kill it.
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    cmd = resume_cmd(s, fork=True)
    assert cmd == "cd /tmp/proj && claude --resume sid1 --fork-session"


def test_resume_cmd_current_no_kill():
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=True, pid=4242)
    cmd = resume_cmd(s)
    assert cmd == "cd /tmp/proj && claude --resume sid1"


def test_resume_cmd_alive_no_pid_omits_kill():
    # L7: should_kill is True (alive, non-current, not fork) but pid is unknown ->
    # the kill segment must be omitted (never emit a bare `kill None`).
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=None)
    assert resume_cmd(s) == "cd /tmp/proj && claude --resume sid1"


def test_resume_cmd_quotes_cwd_with_spaces():
    s = _make_session(sid="sid1", cwd="/tmp/project with space", alive=False)
    cmd = resume_cmd(s)
    assert cmd == "cd '/tmp/project with space' && claude --resume sid1"


# --- D2: terminate_session owns liveness-cache invalidation ---

def test_terminate_session_invalidates_cache(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "invalidate": 0}
    monkeypatch.setattr(so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))

    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    assert so.terminate_session(s) is True
    assert calls["kill"] == 1
    assert calls["invalidate"] == 1


# --- D3: relaunch_in_tmux (搬进 tmux + 远控) ---

def test_tmux_resume_cmd_dead():
    from cc_session_control.actions.session_ops import tmux_resume_cmd
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert tmux_resume_cmd(s) == (
        "cd /tmp/proj && claude --resume abcdef0123456789 --remote-control proj-abcdef01"
    )


def test_tmux_resume_cmd_fork_includes_fork_flag():
    from cc_session_control.actions.session_ops import tmux_resume_cmd
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert tmux_resume_cmd(s, fork=True) == (
        "cd /tmp/proj && claude --resume abcdef0123456789 --fork-session "
        "--remote-control proj-abcdef01"
    )


def test_tmux_resume_cmd_quotes_cwd_and_remote_name():
    from cc_session_control.actions.session_ops import tmux_resume_cmd
    s = _make_session(
        sid="abcdef0123456789",
        cwd="/tmp/project with space",
        alive=False,
    )
    assert tmux_resume_cmd(s) == (
        "cd '/tmp/project with space' && claude --resume abcdef0123456789 "
        "--remote-control 'project with space-abcdef01'"
    )


def test_relaunch_in_tmux_kills_live_non_current(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "invalidate": 0, "tmux": None}
    monkeypatch.setattr(so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))
    monkeypatch.setattr(so.rc, "run_in_tmux",
                        lambda session, window, cmd: calls.__setitem__("tmux", (session, window, cmd)) or True)

    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    assert so.relaunch_in_tmux(s) is True
    assert calls["kill"] == 1
    assert calls["invalidate"] == 1
    _session, window, cmd = calls["tmux"]
    assert window == "proj-abcdef01"
    assert "--resume abcdef0123456789" in cmd
    assert "--remote-control proj-abcdef01" in cmd


def test_relaunch_in_tmux_dead_no_kill(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0}
    monkeypatch.setattr(so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: None)
    monkeypatch.setattr(so.rc, "run_in_tmux", lambda *a: True)

    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert so.relaunch_in_tmux(s) is True
    assert calls["kill"] == 0


# --- M1: resume/relaunch kill paths gated on R10 (no /proc => no kill) ---

def test_relaunch_in_tmux_refuses_kill_without_proc(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "tmux": 0}
    monkeypatch.setattr(so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: None)
    monkeypatch.setattr(so.rc, "run_in_tmux",
                        lambda *a: calls.__setitem__("tmux", calls["tmux"] + 1) or True)
    monkeypatch.setattr(so.proc, "has_proc", lambda: False)

    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    assert so.relaunch_in_tmux(s) is False  # refused: can't confirm current
    assert calls["kill"] == 0               # no SIGTERM while current undeterminable
    assert calls["tmux"] == 0               # and no relaunch either


def test_do_resume_refuses_kill_without_proc(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "exec": 0, "chdir": 0}
    monkeypatch.setattr(so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(so.os, "execvp", lambda *_: calls.__setitem__("exec", calls["exec"] + 1))
    monkeypatch.setattr(so.os, "chdir", lambda *_: calls.__setitem__("chdir", calls["chdir"] + 1))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so.proc, "has_proc", lambda: False)

    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    so.do_resume(s)
    assert calls["kill"] == 0  # refused — never SIGTERM the (undeterminable) current
    assert calls["exec"] == 0  # and does not take over


def test_run_in_tmux_reports_new_window_failure(monkeypatch):
    from cc_session_control.data import rc

    def fake_tmux(args):
        if args[0] == "has-session":
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if args[0] == "new-window":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "failed")
        raise AssertionError(args)

    monkeypatch.setattr(rc, "_tmux_run", fake_tmux)

    assert rc.run_in_tmux("rc", "proj", "cmd") is False


def test_run_in_tmux_reports_new_session_failure(monkeypatch):
    from cc_session_control.data import rc

    def fake_tmux(args):
        if args[0] == "has-session":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "missing")
        if args[0] == "new-session":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "failed")
        raise AssertionError(args)

    monkeypatch.setattr(rc, "_tmux_run", fake_tmux)

    assert rc.run_in_tmux("rc", "proj", "cmd") is False


def test_start_one_quotes_directory_and_remote_name(tmp_path, monkeypatch):
    from cc_session_control.data import rc

    proj = "project with space"
    (tmp_path / proj).mkdir()
    calls = {}
    monkeypatch.setattr(rc.cfg, "workspace", tmp_path)
    monkeypatch.setattr(rc, "is_trusted", lambda name: True)
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [])
    monkeypatch.setattr(rc, "_tmux_has_session", lambda session: False)
    monkeypatch.setattr(
        rc,
        "_tmux_new_session",
        lambda session, window, cmd: calls.__setitem__("cmd", cmd) or True,
    )

    assert rc.start_one(proj) is True

    assert f"cd '{tmp_path / proj}'" in calls["cmd"]
    assert "while true" not in calls["cmd"]
    assert "exec claude remote-control" in calls["cmd"]
    assert "--name 'ws/project with space'" in calls["cmd"]


def test_start_one_refuses_running_window(tmp_path, monkeypatch):
    from cc_session_control.data import rc

    proj = "proj"
    (tmp_path / proj).mkdir()
    calls = {"kill": 0, "new": 0}
    monkeypatch.setattr(rc.cfg, "workspace", tmp_path)
    monkeypatch.setattr(rc, "is_trusted", lambda name: True)
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [proj])
    monkeypatch.setattr(rc, "_is_alive", lambda name: True)
    monkeypatch.setattr(
        rc,
        "stop_one",
        lambda name: calls.__setitem__("kill", calls["kill"] + 1) or True,
    )
    monkeypatch.setattr(
        rc,
        "_tmux_new_window",
        lambda *a: calls.__setitem__("new", calls["new"] + 1) or True,
    )

    assert rc.start_one(proj) is False
    assert calls == {"kill": 0, "new": 0}


def test_start_one_replaces_dead_window(tmp_path, monkeypatch):
    from cc_session_control.data import rc

    proj = "proj"
    (tmp_path / proj).mkdir()
    calls = {"kill": 0, "cmd": None}
    monkeypatch.setattr(rc.cfg, "workspace", tmp_path)
    monkeypatch.setattr(rc, "is_trusted", lambda name: True)
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [proj])
    monkeypatch.setattr(rc, "_is_alive", lambda name: False)
    monkeypatch.setattr(
        rc,
        "stop_one",
        lambda name: calls.__setitem__("kill", calls["kill"] + 1) or True,
    )
    monkeypatch.setattr(rc, "_tmux_has_session", lambda session: True)
    monkeypatch.setattr(
        rc,
        "_tmux_new_window",
        lambda session, window, cmd: calls.__setitem__("cmd", cmd) or True,
    )

    assert rc.start_one(proj) is True
    assert calls["kill"] == 1
    assert calls["cmd"] is not None


# --- D1: cleanup_stats ---

def test_cleanup_stats_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    # H1 self-fetch reaches alive_map/registry; keep it hermetic (no subprocess).
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    registry.invalidate_cache()
    sessions = [
        _make_session(sid="empty1", prompts=0),
        _make_session(sid="short1", prompts=1),
        _make_session(sid="short2", prompts=2),
        _make_session(sid="full1", prompts=5),
    ]
    # session-env: one matching sid (full1), one orphan dir
    env = tmp_path / "session-env"
    env.mkdir()
    (env / "full1").mkdir()
    (env / "orphan-xyz").mkdir()

    stats = cleanup_stats(sessions)
    assert stats["total"] == 4
    assert stats["empty"] == 1
    assert stats["short"] == 2
    assert stats["orphans"] == 1


def test_cleanup_stats_no_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "claude_home", tmp_path)
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    registry.invalidate_cache()
    sessions = [_make_session(sid="full1", prompts=5)]
    stats = cleanup_stats(sessions)
    assert stats == {"total": 1, "empty": 0, "short": 0, "orphans": 0}


# --- D4: _parse_transcript ---

def _write_jsonl(tmp_path, sid, lines):
    # Compact separators so the '"type":"user"' substring pre-check in
    # _parse_transcript matches, mirroring Claude's actual transcript format.
    f = tmp_path / f"{sid}.jsonl"
    f.write_text(
        "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n"
    )
    return str(f)


def test_parse_transcript_basic_fields(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"type": "user", "message": {"content": "hello world"}},
        {"type": "user", "message": {"content": "second prompt"}},
    ])
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s is not None
    assert s.sid == "sid1"
    assert s.cwd == "/tmp/proj"
    assert s.prompts == 2
    assert s.pid is None
    assert s.alive is False
    assert s.current is False
    assert s.file == path


def test_parse_transcript_none_when_no_cwd(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"type": "user", "message": {"content": "hello"}},
    ])
    assert _parse_transcript(path, idx={}, cur=set(), job_shorts=set()) is None


def test_parse_transcript_label_priority_aititle(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"aiTitle": "The Title"},
        {"lastPrompt": "the last prompt"},
        {"type": "user", "message": {"content": "first prompt"}},
    ])
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "The Title"


def test_parse_transcript_label_priority_first_prompt(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"lastPrompt": "the last prompt"},
        {"type": "user", "message": {"content": "first real prompt"}},
    ])
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "first real prompt"


def test_parse_transcript_label_priority_last_prompt(tmp_path):
    # No aiTitle, and the only user prompt is noise -> falls back to lastPrompt.
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"lastPrompt": "the last prompt"},
        {"type": "user", "message": {"content": "<system-reminder>noise</system-reminder>"}},
    ])
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "the last prompt"


def test_parse_transcript_label_untitled(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
    ])
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "(untitled)"


def test_parse_transcript_alive_and_current(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    idx = {"sid1": LiveInfo(sid="sid1", pid=4242, alive=True)}
    s = _parse_transcript(path, idx=idx, cur={4242}, job_shorts=set())
    assert s.pid == 4242
    assert s.alive is True
    assert s.current is True


def test_parse_transcript_current_via_older_alive_pid(tmp_path):
    # Flag ① — multi-pid under-protection. A resumed sid has two alive pids;
    # the NEWEST (710575) is chosen for display, but csctl was launched by the
    # OLDER one (700772). `current` must still be True so the session stays
    # protected — the old `pid in cur` check (pid==710575) would miss it.
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    idx = {
        "sid1": LiveInfo(
            sid="sid1", pid=710575, pids=[700772, 710575], alive=True
        )
    }
    s = _parse_transcript(path, idx=idx, cur={700772}, job_shorts=set())
    assert s.pid == 710575          # newest chosen for display
    assert s.current is True        # older ancestor pid still protects it


def test_parse_transcript_rc_exposed_requires_proc_alive(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    idx = {
        "sid1": LiveInfo(
            sid="sid1",
            pid=4242,
            alive=True,
            proc_alive=False,
            bridge="session_env",
        )
    }
    s = _parse_transcript(path, idx=idx, cur=set(), job_shorts=set())
    assert s.alive is True
    assert s.rc_exposed is False
    assert s.env_id is None


def test_parse_transcript_sets_rc_exposed_when_proc_alive(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    idx = {
        "sid1": LiveInfo(
            sid="sid1",
            pid=4242,
            alive=True,
            proc_alive=True,
            bridge="session_env",
        )
    }
    s = _parse_transcript(path, idx=idx, cur=set(), job_shorts=set())
    assert s.rc_exposed is True
    assert s.env_id == "session_env"


def test_parse_transcript_hidden_tags(tmp_path):
    path = _write_jsonl(tmp_path, "sid1", [
        {"cwd": "/tmp/proj", "kind": "sdk-ts"},
        {"note": "bridge-session"},
        {"type": "user", "message": {"content": "hi"}},
    ])
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.hidden == {"sdk", "bridge"}
