"""Data-layer unit tests — pure functions, transcript parsing, rc toggles."""

import time

import json
import subprocess

from cc_session_control.actions.session_ops import resume_cmd
from cc_session_control.data.cleanup import prune_sessions
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
    monkeypatch.setattr(so.proc, "pid_alive", lambda pid, start: True)
    monkeypatch.setattr(so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))

    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    assert so.terminate_session(s) is True
    assert calls["kill"] == 1
    assert calls["invalidate"] == 1


# --- take_over: the ONE kill primitive (gate → recheck → SIGTERM → settle) ---

def test_take_over_refused_without_proc(monkeypatch):
    import cc_session_control.actions.session_ops as so
    monkeypatch.setattr(so.proc, "current_determinable", lambda: False)
    monkeypatch.setattr(so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill")))
    assert so.take_over(4242) == "refused"


def test_take_over_skips_kill_when_pid_gone_or_recycled(monkeypatch):
    # Kill-time recheck: a pid that died (or was recycled — proc_start mismatch)
    # while the confirm modal sat open must NOT be SIGTERMed.
    import cc_session_control.actions.session_ops as so
    monkeypatch.setattr(so.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(so.proc, "pid_alive", lambda pid, start: False)
    monkeypatch.setattr(so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill")))
    inv = {"n": 0}
    monkeypatch.setattr(so, "invalidate_cache", lambda: inv.__setitem__("n", inv["n"] + 1))
    assert so.take_over(4242, "12345") == "gone"
    assert inv["n"] == 1


def test_take_over_failed_on_signal_error(monkeypatch):
    import cc_session_control.actions.session_ops as so
    monkeypatch.setattr(so.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(so.proc, "pid_alive", lambda pid, start: True)

    def raise_perm(*_):
        raise PermissionError("nope")

    monkeypatch.setattr(so.os, "kill", raise_perm)
    assert so.take_over(4242) == "failed"


def test_take_over_kills_settles_and_invalidates(monkeypatch):
    import cc_session_control.actions.session_ops as so
    calls = {"kill": None, "sleep": 0, "invalidate": 0}
    monkeypatch.setattr(so.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(so.proc, "pid_alive", lambda pid, start: True)
    monkeypatch.setattr(so.os, "kill", lambda pid, sig: calls.__setitem__("kill", (pid, sig)))
    monkeypatch.setattr(so.time, "sleep", lambda *_: calls.__setitem__("sleep", calls["sleep"] + 1))
    monkeypatch.setattr(so, "invalidate_cache", lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))
    assert so.take_over(4242, "999") == "killed"
    assert calls["kill"] == (4242, so.signal.SIGTERM)
    assert calls["sleep"] == 1
    assert calls["invalidate"] == 1


# --- tmux-first dispatch: tmux resume / attach (ADR-0001) ---

def test_tmux_foreground_cmd_no_remote_control():
    from cc_session_control.actions.session_ops import tmux_foreground_cmd
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert tmux_foreground_cmd(s) == "cd /tmp/proj && claude --resume abcdef0123456789"


def test_tmux_foreground_cmd_fork_includes_fork_flag():
    from cc_session_control.actions.session_ops import tmux_foreground_cmd
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert tmux_foreground_cmd(s, fork=True) == (
        "cd /tmp/proj && claude --resume abcdef0123456789 --fork-session"
    )


def test_tmux_foreground_cmd_quotes_cwd():
    from cc_session_control.actions.session_ops import tmux_foreground_cmd
    s = _make_session(sid="sid1", cwd="/tmp/project with space", alive=False)
    assert tmux_foreground_cmd(s) == "cd '/tmp/project with space' && claude --resume sid1"


def test_attach_target_dead_session_is_none():
    from cc_session_control.actions.session_ops import attach_target
    # Even a stale tmux_target must not answer for a dead session.
    s = _make_session(sid="sid1", alive=False, pid=None, tmux_target="cc:3")
    assert attach_target(s) is None


def test_attach_target_reads_snapshot_field():
    # attach_target is a pure read of the snapshot-computed Session.tmux_target
    # (same source as the ⧉ badge) — no per-action tmux re-detection.
    from cc_session_control.actions.session_ops import attach_target
    hosted = _make_session(sid="sid1", alive=True, current=False, pid=4242,
                           tmux_target="cc:3")
    bare = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    assert attach_target(hosted) == "cc:3"
    assert attach_target(bare) is None


def test_window_containing_matches_ancestor():
    from cc_session_control.data.tmux import window_containing
    panes = [("cc:1", 100), ("rc:0", 200)]
    assert window_containing(panes, {4242, 200}) == "rc:0"
    assert window_containing(panes, {4242}) is None
    assert window_containing([], {100}) is None


def test_residency_targets_batch_join(monkeypatch):
    # ONE list-panes call for the whole pid set; per-pid ancestor-chain match.
    from cc_session_control.data import tmux
    calls = {"panes": 0}
    panes = [("proj:1", 100), ("other:2", 200)]
    monkeypatch.setattr(tmux, "_tmux_list_all_panes",
                        lambda: calls.__setitem__("panes", calls["panes"] + 1) or panes)
    ancestors = {4242: {100, 1}, 4343: {200, 1}, 5555: {999}}
    monkeypatch.setattr(tmux.proc, "ancestors_of", lambda pid: ancestors.get(pid, set()))

    out = tmux.residency_targets([4242, 4343, 5555])

    assert out == {4242: "proj:1", 4343: "other:2"}  # 5555: no hit -> absent
    assert calls["panes"] == 1                        # one tmux subprocess total


def test_residency_targets_empty_pids_skips_tmux(monkeypatch):
    from cc_session_control.data import tmux
    monkeypatch.setattr(tmux, "_tmux_list_all_panes",
                        lambda: (_ for _ in ()).throw(AssertionError("no tmux call")))
    assert tmux.residency_targets([]) == {}


def test_residency_targets_tmux_failure_returns_empty(monkeypatch):
    from cc_session_control.data import tmux
    monkeypatch.setattr(tmux, "_tmux_list_all_panes", lambda: [])
    assert tmux.residency_targets([4242]) == {}


def test_find_session_window_first_hit_over_residency(monkeypatch):
    # find_session_window is now the single-target convenience over
    # residency_targets — first hit in pids order, None on no hit.
    from cc_session_control.data import tmux
    monkeypatch.setattr(tmux, "residency_targets",
                        lambda pids: {4343: "other:2", 4242: "proj:1"})
    assert tmux.find_session_window([4242, 4343]) == "proj:1"
    monkeypatch.setattr(tmux, "residency_targets", lambda pids: {})
    assert tmux.find_session_window([4242]) is None


def test_do_tmux_resume_kills_live_non_current(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": [], "spawn": []}
    monkeypatch.setattr(so.os, "kill", lambda pid, sig: calls["kill"].append(pid))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: None)
    monkeypatch.setattr(so.proc, "current_determinable", lambda: True)
    monkeypatch.setattr(so.proc, "pid_alive", lambda pid, start: True)
    monkeypatch.setattr(
        so.tmux, "run_in_tmux",
        lambda session, window, cmd: calls["spawn"].append((session, window, cmd)) or f"{session}:1",
    )
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    target = so.do_tmux_resume(s)
    assert calls["kill"] == [4242]
    assert target == "proj:1"  # per-project session, exact spawned target
    session, window, cmd = calls["spawn"][0]
    assert session == "proj"
    assert window == "abcdef01"
    assert "--remote-control" not in cmd


def test_do_tmux_resume_dead_session_no_kill(monkeypatch):
    import cc_session_control.actions.session_ops as so
    monkeypatch.setattr(so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill")))
    monkeypatch.setattr(so.tmux, "run_in_tmux", lambda session, window, cmd: f"{session}:0")
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert so.do_tmux_resume(s) == "proj:0"


def test_do_tmux_resume_refuses_takeover_when_degraded(monkeypatch):
    import cc_session_control.actions.session_ops as so
    monkeypatch.setattr(so.proc, "current_determinable", lambda: False)
    monkeypatch.setattr(so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill")))
    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    assert so.do_tmux_resume(s) is None


def test_do_tmux_new_spawns_and_returns_target(monkeypatch):
    import cc_session_control.actions.session_ops as so

    spawns = []
    monkeypatch.setattr(so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill")))
    monkeypatch.setattr(
        so.tmux, "run_in_tmux",
        lambda session, window, cmd: spawns.append((session, window, cmd)) or f"{session}:0",
    )
    target = so.do_tmux_new("/tmp/proj with space")
    assert target == "proj with space:0"
    session, window, cmd = spawns[0]
    assert session == "proj with space"  # per-project session = dir basename
    assert window == "claude"
    assert cmd == "cd '/tmp/proj with space' && claude"
    assert "--remote-control" not in cmd and "--resume" not in cmd


def test_do_tmux_new_spawn_failure_returns_none(monkeypatch):
    import cc_session_control.actions.session_ops as so
    monkeypatch.setattr(so.tmux, "run_in_tmux", lambda *a: None)
    assert so.do_tmux_new("/tmp/proj") is None


def test_do_tmux_resume_fork_spawns_fork_window_no_kill(monkeypatch):
    # A fork is a copy: never kills, and gets its own <sid8>-fork window so it
    # doesn't shadow the original session's window.
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "tmux": None}
    monkeypatch.setattr(so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: None)
    monkeypatch.setattr(so.tmux, "run_in_tmux",
                        lambda session, window, cmd: calls.__setitem__("tmux", (session, window, cmd)) or f"{session}:2")

    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    assert so.do_tmux_resume(s, fork=True) == "proj:2"
    assert calls["kill"] == 0             # fork leaves the original running
    session, window, cmd = calls["tmux"]
    assert session == "proj"
    assert window == "abcdef01-fork"
    assert "--fork-session" in cmd
    assert "--remote-control" not in cmd


# --- M1: resume kill paths gated on R10 (no /proc => no kill) ---


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
    from cc_session_control.data import tmux

    def fake_tmux(args):
        if args[0] == "has-session":
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if args[0] == "new-window":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "failed")
        raise AssertionError(args)

    monkeypatch.setattr(tmux, "_tmux_run", fake_tmux)

    assert tmux.run_in_tmux("rc", "proj", "cmd") is None


def test_run_in_tmux_reports_new_session_failure(monkeypatch):
    from cc_session_control.data import tmux

    def fake_tmux(args):
        if args[0] == "has-session":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "missing")
        if args[0] == "new-session":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "failed")
        raise AssertionError(args)

    monkeypatch.setattr(tmux, "_tmux_run", fake_tmux)

    assert tmux.run_in_tmux("rc", "proj", "cmd") is None


def test_run_in_tmux_returns_printed_target(monkeypatch):
    from cc_session_control.data import tmux

    def fake_tmux(args):
        if args[0] == "has-session":
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if args[0] == "new-window":
            assert "-P" in args  # exact-target contract
            return subprocess.CompletedProcess(["tmux", *args], 0, "proj:3\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(tmux, "_tmux_run", fake_tmux)

    assert tmux.run_in_tmux("proj", "claude", "cmd") == "proj:3"


def test_session_name_for_sanitizes_tmux_separators():
    from cc_session_control.data.tmux import session_name_for
    assert session_name_for("/tmp/myproj") == "myproj"
    assert session_name_for("/tmp/myproj/") == "myproj"
    assert session_name_for("/tmp/my.proj") == "my-proj"
    assert session_name_for("/tmp/a:b.c") == "a-b-c"
    assert session_name_for("") == "claude"


def test_list_windows_meta_parses_and_prefers_declared_path(monkeypatch):
    from types import SimpleNamespace

    from cc_session_control.data import tmux

    out = (
        "@1\tfoo\t0\t111\t/declared\t/current\n"
        "@2\tbar\t1\t222\t\t/fallback\n"
        "@3\tbaz\t0\tnope\t\t\n"
        "bogus-line\n"
    )
    monkeypatch.setattr(
        tmux, "_tmux_run", lambda args: SimpleNamespace(returncode=0, stdout=out),
    )

    wins = tmux.list_windows_meta("rc")
    assert wins[0] == tmux.TmuxWindow("@1", "foo", False, 111, "/declared")
    assert wins[1] == tmux.TmuxWindow("@2", "bar", True, 222, "/fallback")
    assert wins[2].pid is None and wins[2].path == ""
    assert len(wins) == 3                 # malformed line skipped


def test_start_one_quotes_directory_and_remote_name(tmp_path, monkeypatch):
    from cc_session_control.data import rc, tmux

    proj = tmp_path / "project with space"
    proj.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({
        "projects": {str(proj): {"hasTrustDialogAccepted": True}},
    }))
    calls = {}
    opts = {}
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(rc, "_tmux_windows", lambda: [])
    monkeypatch.setattr(tmux, "_tmux_has_session", lambda session: False)
    monkeypatch.setattr(
        tmux,
        "_tmux_new_session",
        lambda session, window, cmd: calls.__setitem__("cmd", cmd) or "rc:1",
    )
    monkeypatch.setattr(
        tmux,
        "set_window_option",
        lambda target, option, value: opts.__setitem__(option, (target, value)) or True,
    )

    assert rc.start_one(str(proj)) is True

    assert f"cd '{proj}'" in calls["cmd"]
    assert "while true" not in calls["cmd"]
    assert "exec claude remote-control" in calls["cmd"]
    assert "--name 'project with space'" in calls["cmd"]
    # The window declares its project path — the join key scan/stop read back.
    assert opts["@csctl_path"] == ("rc:1", str(proj))


def test_start_one_refuses_running_window(tmp_path, monkeypatch):
    from cc_session_control.data import rc, tmux

    proj = tmp_path / "proj"
    proj.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({
        "projects": {str(proj): {"hasTrustDialogAccepted": True}},
    }))
    calls = {"kill": 0, "new": 0}
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(
        rc, "_tmux_windows",
        lambda: [tmux.TmuxWindow("@1", "proj", False, 1, str(proj))],
    )
    monkeypatch.setattr(
        rc,
        "stop_one",
        lambda path: calls.__setitem__("kill", calls["kill"] + 1) or True,
    )
    monkeypatch.setattr(
        tmux,
        "_tmux_new_window",
        lambda *a: calls.__setitem__("new", calls["new"] + 1) or "rc:1",
    )

    assert rc.start_one(str(proj)) is False
    assert calls == {"kill": 0, "new": 0}


def test_start_one_replaces_dead_window(tmp_path, monkeypatch):
    from cc_session_control.data import rc, tmux

    proj = tmp_path / "proj"
    proj.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({
        "projects": {str(proj): {"hasTrustDialogAccepted": True}},
    }))
    calls = {"kill": 0, "cmd": None}
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(
        rc, "_tmux_windows",
        lambda: [tmux.TmuxWindow("@1", "proj", True, 1, str(proj))],
    )
    monkeypatch.setattr(
        rc,
        "stop_one",
        lambda path: calls.__setitem__("kill", calls["kill"] + 1) or True,
    )
    monkeypatch.setattr(tmux, "_tmux_has_session", lambda session: True)
    monkeypatch.setattr(
        tmux,
        "_tmux_new_window",
        lambda session, window, cmd: calls.__setitem__("cmd", cmd) or "rc:2",
    )
    monkeypatch.setattr(tmux, "set_window_option", lambda *a: True)

    assert rc.start_one(str(proj)) is True
    assert calls["kill"] == 1
    assert calls["cmd"] is not None


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
