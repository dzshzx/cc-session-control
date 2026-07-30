"""Data-layer unit tests — pure functions, transcript parsing, rc toggles."""

import json
import subprocess
import time

from factories import make_session

from cc_session_control.actions.session_ops import resume_cmd
from cc_session_control.data.cleanup import prune_sessions
from cc_session_control.data.sessions import _parse_transcript
from cc_session_control.models import LiveInfo


def _created_target(tmux, target):
    return tmux.TmuxWriteResult(
        tmux.TmuxWriteStage.NEW_WINDOW,
        tmux.TmuxWriteState.SUCCEEDED,
        target=target,
    )


def _create_failure(tmux, detail="tmux unavailable"):
    return tmux.TmuxWriteResult(
        tmux.TmuxWriteStage.NEW_WINDOW,
        tmux.TmuxWriteState.FAILED,
        detail=detail,
    )


def _metadata_written(tmux, target):
    return tmux.TmuxWriteResult(
        tmux.TmuxWriteStage.WINDOW_OPTION,
        tmux.TmuxWriteState.SUCCEEDED,
        target=target,
    )


_make_session = make_session


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
    assert cmd == "csctl resume --take-over sid1"
    assert "4242" not in cmd
    assert "kill" not in cmd


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
    assert cmd == "csctl resume --take-over sid1"
    assert "4242" not in cmd
    assert "kill" not in cmd


def test_resume_cmd_alive_no_pid_omits_kill():
    # A live row never serializes its incomplete execution evidence into a
    # direct resume command. The execution-time resolver will fail closed.
    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=None)
    assert resume_cmd(s) == "csctl resume --take-over sid1"


def test_resume_cmd_quotes_cwd_with_spaces():
    s = _make_session(sid="sid1", cwd="/tmp/project with space", alive=False)
    cmd = resume_cmd(s)
    assert cmd == "cd '/tmp/project with space' && claude --resume sid1"


# --- take_over: the ONE kill primitive (gate → recheck → SIGTERM → settle) ---


def test_take_over_refused_without_proc(monkeypatch):
    import cc_session_control.actions.session_ops as so

    issue = so.proc.ProcIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset(), (issue,)),
    )
    monkeypatch.setattr(
        so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill"))
    )
    assert so.take_over_result(4242).state is so.TakeOverState.REFUSED


def test_take_over_skips_kill_when_pid_gone_or_recycled(monkeypatch):
    # Kill-time recheck: a pid that died (or was recycled — proc_start mismatch)
    # while the confirm modal sat open must NOT be SIGTERMed.
    import cc_session_control.actions.session_ops as so

    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(
        so.proc,
        "probe_pid",
        lambda pid, start: so.proc.PidProbe(pid, False),
    )
    monkeypatch.setattr(
        so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill"))
    )
    inv = {"n": 0}
    monkeypatch.setattr(
        so, "invalidate_cache", lambda: inv.__setitem__("n", inv["n"] + 1)
    )
    assert so.take_over_result(4242, "12345").state is so.TakeOverState.GONE
    assert inv["n"] == 1


def test_take_over_failed_on_signal_error(monkeypatch):
    import cc_session_control.actions.session_ops as so

    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(
        so.proc,
        "probe_pid",
        lambda pid, start: so.proc.PidProbe(pid, True),
    )

    def raise_perm(*_):
        raise PermissionError("nope")

    monkeypatch.setattr(so.os, "kill", raise_perm)
    assert so.take_over_result(4242).state is so.TakeOverState.FAILED


def test_take_over_kills_settles_and_invalidates(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": None, "sleep": 0, "invalidate": 0}
    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(
        so.proc,
        "probe_pid",
        lambda pid, start: so.proc.PidProbe(pid, True),
    )
    monkeypatch.setattr(
        so.os, "kill", lambda pid, sig: calls.__setitem__("kill", (pid, sig))
    )
    monkeypatch.setattr(
        so.time, "sleep", lambda *_: calls.__setitem__("sleep", calls["sleep"] + 1)
    )
    monkeypatch.setattr(
        so,
        "invalidate_cache",
        lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1),
    )
    assert so.take_over_result(4242, "999").state is so.TakeOverState.KILLED
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
    assert (
        tmux_foreground_cmd(s) == "cd '/tmp/project with space' && claude --resume sid1"
    )


def test_attach_target_dead_session_is_none():
    from cc_session_control.actions.session_ops import attach_target

    # Even a stale tmux_target must not answer for a dead session.
    s = _make_session(sid="sid1", alive=False, pid=None, tmux_target="cc:3")
    assert attach_target(s) is None


def test_attach_target_reads_snapshot_field():
    # attach_target is a pure read of the snapshot-computed Session.tmux_target
    # (same source as the ⧉ badge) — no per-action tmux re-detection.
    from cc_session_control.actions.session_ops import attach_target

    hosted = _make_session(
        sid="sid1", alive=True, current=False, pid=4242, tmux_target="cc:3"
    )
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
    panes = [tmux.TmuxPane("proj:1", 100), tmux.TmuxPane("other:2", 200)]
    monkeypatch.setattr(
        tmux,
        "list_panes_inventory",
        lambda: (
            calls.__setitem__("panes", calls["panes"] + 1)
            or tmux.PaneInventory(tuple(panes))
        ),
    )
    ancestors = {4242: {100, 1}, 4343: {200, 1}, 5555: {999}}
    monkeypatch.setattr(
        tmux.proc,
        "probe_ancestors",
        lambda pid: tmux.proc.AncestorProbe(frozenset(ancestors.get(pid, set()))),
    )

    out = tmux.residency_inventory([4242, 4343, 5555])

    assert dict(out.targets) == {
        4242: "proj:1",
        4343: "other:2",
    }  # 5555: no hit -> absent
    assert calls["panes"] == 1  # one tmux subprocess total


def test_residency_targets_empty_pids_skips_tmux(monkeypatch):
    from cc_session_control.data import tmux

    monkeypatch.setattr(
        tmux,
        "list_panes_inventory",
        lambda: (_ for _ in ()).throw(AssertionError("no tmux call")),
    )
    assert dict(tmux.residency_inventory([]).targets) == {}


def test_residency_targets_tmux_failure_returns_empty(monkeypatch):
    from cc_session_control.data import tmux

    monkeypatch.setattr(
        tmux,
        "list_panes_inventory",
        lambda: tmux.PaneInventory(
            issues=(
                tmux.ResidencyIssue(
                    "tmux list-panes",
                    None,
                    "lost server connection",
                ),
            )
        ),
    )
    assert dict(tmux.residency_inventory([4242]).targets) == {}


def test_find_session_window_first_hit_over_residency(monkeypatch):
    # find_session_window_result is the typed first-target view over the
    # residency result — first hit in pids order, None on no hit.
    from cc_session_control.data import tmux

    monkeypatch.setattr(
        tmux,
        "residency_inventory",
        lambda _pids: tmux.ResidencyInventory({4343: "other:2", 4242: "proj:1"}),
    )
    assert tmux.find_session_window_result([4242, 4343]).target == "proj:1"
    monkeypatch.setattr(
        tmux,
        "residency_inventory",
        lambda _pids: tmux.ResidencyInventory(),
    )
    assert tmux.find_session_window_result([4242]).target is None


def test_do_tmux_resume_kills_live_non_current(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": [], "spawn": []}
    monkeypatch.setattr(so.os, "kill", lambda pid, sig: calls["kill"].append(pid))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: None)
    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(
        so.proc,
        "probe_pid",
        lambda pid, start: so.proc.PidProbe(pid, True),
    )
    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: (
            calls["spawn"].append((session, window, cmd))
            or _created_target(so.tmux, f"{session}:1")
        ),
    )
    s = _make_session(
        sid="abcdef0123456789",
        cwd="/tmp/proj",
        alive=True,
        current=False,
        pid=4242,
        proc_start="known-start",
    )

    def resolve_execution_session(sid: str) -> so.ExecutionSessionResolution:
        assert sid == s.sid
        return so.ExecutionSessionResolution(
            so.ExecutionSessionState.RESOLVED,
            session=s,
        )

    monkeypatch.setattr(
        so,
        "resolve_execution_session",
        resolve_execution_session,
    )
    target = so.do_tmux_resume_result(s).target
    assert calls["kill"] == [4242]
    assert target == "proj:1"  # per-project session, exact spawned target
    session, window, cmd = calls["spawn"][0]
    assert session == "proj"
    assert window == "abcdef01"
    assert "--remote-control" not in cmd


def test_do_tmux_resume_dead_session_no_kill(monkeypatch):
    import cc_session_control.actions.session_ops as so

    monkeypatch.setattr(
        so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill"))
    )
    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: _created_target(so.tmux, f"{session}:0"),
    )
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert so.do_tmux_resume_result(s).target == "proj:0"


def test_do_tmux_resume_refuses_takeover_when_degraded(monkeypatch):
    import cc_session_control.actions.session_ops as so

    issue = so.proc.ProcIssue("process ancestors", "/proc", "unavailable")
    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset(), (issue,)),
    )
    monkeypatch.setattr(
        so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill"))
    )
    s = _make_session(sid="sid1", alive=True, current=False, pid=4242)
    assert so.do_tmux_resume_result(s).target is None


def test_do_tmux_new_spawns_and_returns_target(monkeypatch):
    import cc_session_control.actions.session_ops as so

    spawns = []
    monkeypatch.setattr(
        so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill"))
    )
    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: (
            spawns.append((session, window, cmd))
            or _created_target(so.tmux, f"{session}:0")
        ),
    )
    result = so.do_tmux_new_result("/tmp/proj with space")
    assert result.success is True
    assert result.target == "proj with space:0"
    session, window, cmd = spawns[0]
    assert session == "proj with space"  # per-project session = dir basename
    assert window == "claude"
    assert cmd == "cd '/tmp/proj with space' && claude"
    assert "--remote-control" not in cmd and "--resume" not in cmd


def test_do_tmux_new_spawn_failure_returns_none(monkeypatch):
    import cc_session_control.actions.session_ops as so

    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda *a: _create_failure(so.tmux),
    )
    result = so.do_tmux_new_result("/tmp/proj")
    assert result.success is False
    assert result.target is None


def test_do_tmux_resume_fork_spawns_fork_window_no_kill(monkeypatch):
    # A fork is a copy: never kills, and gets its own <sid8>-fork window so it
    # doesn't shadow the original session's window.
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "tmux": None}
    monkeypatch.setattr(
        so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1)
    )
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: None)
    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: (
            calls.__setitem__("tmux", (session, window, cmd))
            or _created_target(so.tmux, f"{session}:2")
        ),
    )

    s = _make_session(
        sid="abcdef0123456789", cwd="/tmp/proj", alive=True, current=False, pid=4242
    )
    assert so.do_tmux_resume_result(s, fork=True).target == "proj:2"
    assert calls["kill"] == 0  # fork leaves the original running
    session, window, cmd = calls["tmux"]
    assert session == "proj"
    assert window == "abcdef01-fork"
    assert "--fork-session" in cmd
    assert "--remote-control" not in cmd


# --- M1: resume kill paths gated on R10 (no /proc => no kill) ---


def test_do_resume_refuses_kill_without_proc(monkeypatch):
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "exec": 0, "chdir": 0}
    monkeypatch.setattr(
        so.os, "kill", lambda *_: calls.__setitem__("kill", calls["kill"] + 1)
    )
    monkeypatch.setattr(
        so.os, "execvp", lambda *_: calls.__setitem__("exec", calls["exec"] + 1)
    )
    monkeypatch.setattr(
        so.os, "chdir", lambda *_: calls.__setitem__("chdir", calls["chdir"] + 1)
    )
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so.proc, "has_proc", lambda: False)

    s = _make_session(sid="sid1", cwd="/tmp/proj", alive=True, current=False, pid=4242)
    so.do_resume(s)
    assert calls["kill"] == 0  # refused — never SIGTERM the (undeterminable) current
    assert calls["exec"] == 0  # and does not take over


def test_run_in_tmux_reports_new_window_failure(monkeypatch):
    from cc_session_control.data import tmux

    def fake_tmux(argv, **_kwargs):
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "new-window":
            return subprocess.CompletedProcess(argv, 1, "", "failed")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", fake_tmux)

    assert tmux.run_in_tmux_result("rc", "proj", "cmd").success is False


def test_run_in_tmux_reports_new_session_failure(monkeypatch):
    from cc_session_control.data import tmux

    def fake_tmux(argv, **_kwargs):
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 1, "", "can't find session: rc")
        if argv[1] == "new-session":
            return subprocess.CompletedProcess(argv, 1, "", "failed")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", fake_tmux)

    assert tmux.run_in_tmux_result("rc", "proj", "cmd").success is False


def test_run_in_tmux_returns_printed_target(monkeypatch):
    from cc_session_control.data import tmux

    def fake_tmux(argv, **_kwargs):
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "new-window":
            assert "-P" in argv  # exact-target contract
            return subprocess.CompletedProcess(argv, 0, "proj:3\n", "")
        raise AssertionError(argv)

    monkeypatch.setattr(tmux.subprocess, "run", fake_tmux)

    assert tmux.run_in_tmux_result("proj", "claude", "cmd").target == "proj:3"


def test_session_name_for_sanitizes_tmux_separators():
    from cc_session_control.data.tmux import session_name_for

    assert session_name_for("/tmp/myproj") == "myproj"
    assert session_name_for("/tmp/myproj/") == "myproj"
    assert session_name_for("/tmp/my.proj") == "my-proj"
    assert session_name_for("/tmp/a:b.c") == "a-b-c"
    assert session_name_for("") == "claude"


def test_list_windows_meta_parses_and_prefers_declared_path(monkeypatch):
    from cc_session_control.data import tmux

    out = (
        "@1\tfoo\t0\t111\t/declared\t/current\n"
        "@2\tbar\t1\t222\t\t/fallback\n"
        "@3\tbaz\t0\tnope\t\t\n"
        "bogus-line\n"
    )
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda argv, **_kwargs: tmux.subprocess.CompletedProcess(
            argv,
            0,
            stdout=out,
            stderr="",
        ),
    )

    inventory = tmux.list_windows_inventory("rc")
    assert inventory.records[0] == tmux.TmuxWindow("@1", "foo", False, 111, "/declared")
    assert inventory.records[1] == tmux.TmuxWindow("@2", "bar", True, 222, "/fallback")
    assert len(inventory.records) == 2
    assert inventory.complete is False
    assert len(inventory.issues) == 2


def test_start_one_quotes_directory_and_remote_name(tmp_path, monkeypatch):
    from cc_session_control.data import rc, tmux

    proj = tmp_path / "project with space"
    proj.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "projects": {str(proj): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    calls = {}
    opts = {}
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: tmux.WindowInventory(),
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: (
            calls.__setitem__("cmd", cmd) or _created_target(tmux, "rc:1")
        ),
    )
    monkeypatch.setattr(
        tmux,
        "set_window_option_result",
        lambda target, option, value: (
            opts.__setitem__(option, (target, value)) or _metadata_written(tmux, target)
        ),
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
    claude_json.write_text(
        json.dumps(
            {
                "projects": {str(proj): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    calls = {"kill": 0, "new": 0}
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: tmux.WindowInventory(
            (tmux.TmuxWindow("@1", "proj", False, 1, str(proj)),)
        ),
    )
    monkeypatch.setattr(
        rc,
        "stop_one_result",
        lambda path, *, window_inventory=None: (
            calls.__setitem__("kill", calls["kill"] + 1)
            or rc.StopResult(rc.StopState.STOPPED, path)
        ),
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda *a: (
            calls.__setitem__("new", calls["new"] + 1) or _created_target(tmux, "rc:1")
        ),
    )

    assert rc.start_one(str(proj)) is False
    assert calls == {"kill": 0, "new": 0}


def test_start_one_replaces_dead_window(tmp_path, monkeypatch):
    from cc_session_control.data import rc, tmux

    proj = tmp_path / "proj"
    proj.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "projects": {str(proj): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    calls = {"kill": 0, "cmd": None}
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)
    monkeypatch.setattr(
        rc,
        "_tmux_window_inventory",
        lambda: tmux.WindowInventory(
            (tmux.TmuxWindow("@1", "proj", True, 1, str(proj)),)
        ),
    )
    monkeypatch.setattr(
        rc,
        "stop_one_result",
        lambda path, *, window_inventory=None: (
            calls.__setitem__("kill", calls["kill"] + 1)
            or rc.StopResult(rc.StopState.STOPPED, path)
        ),
    )
    monkeypatch.setattr(
        tmux,
        "run_in_tmux_result",
        lambda session, window, cmd: (
            calls.__setitem__("cmd", cmd) or _created_target(tmux, "rc:2")
        ),
    )
    monkeypatch.setattr(
        tmux,
        "set_window_option_result",
        lambda target, *_a: _metadata_written(tmux, target),
    )

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
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"type": "user", "message": {"content": "hello world"}},
            {"type": "user", "message": {"content": "second prompt"}},
        ],
    )
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
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"type": "user", "message": {"content": "hello"}},
        ],
    )
    assert _parse_transcript(path, idx={}, cur=set(), job_shorts=set()) is None


def test_parse_transcript_label_priority_aititle(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"aiTitle": "The Title"},
            {"lastPrompt": "the last prompt"},
            {"type": "user", "message": {"content": "first prompt"}},
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "The Title"


def test_parse_transcript_label_priority_first_prompt(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"lastPrompt": "the last prompt"},
            {"type": "user", "message": {"content": "first real prompt"}},
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "first real prompt"


def test_parse_transcript_label_priority_last_prompt(tmp_path):
    # No aiTitle, and the only user prompt is noise -> falls back to lastPrompt.
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"lastPrompt": "the last prompt"},
            {
                "type": "user",
                "message": {"content": "<system-reminder>noise</system-reminder>"},
            },
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "the last prompt"


def test_parse_transcript_label_untitled(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.label == "(untitled)"


def test_parse_transcript_alive_and_current(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"type": "user", "message": {"content": "hi"}},
        ],
    )
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
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"type": "user", "message": {"content": "hi"}},
        ],
    )
    idx = {"sid1": LiveInfo(sid="sid1", pid=710575, pids=[700772, 710575], alive=True)}
    s = _parse_transcript(path, idx=idx, cur={700772}, job_shorts=set())
    assert s.pid == 710575  # newest chosen for display
    assert s.current is True  # older ancestor pid still protects it


def test_parse_transcript_rc_exposed_requires_proc_alive(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"type": "user", "message": {"content": "hi"}},
        ],
    )
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
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"type": "user", "message": {"content": "hi"}},
        ],
    )
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
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj", "kind": "sdk-ts"},
            {"note": "bridge-session"},
            {"type": "user", "message": {"content": "hi"}},
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set(), job_shorts=set())
    assert s.hidden == {"sdk", "bridge"}
