"""Core unit tests for cleanup selection, session/tmux actions, and transcripts."""

import json
import subprocess

import pytest
from factories import make_session

from cc_session_control.actions.session_ops import resume_cmd
from cc_session_control.data import sessions as sessions_mod
from cc_session_control.data import transcripts as transcripts_mod
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


_make_session = make_session


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
    # Pre-kill probe reports alive; the post-SIGTERM settle recheck sees the
    # process die on its second poll — KILLED only after that recheck
    # confirms it, and the settle loop stops polling as soon as it does.
    import cc_session_control.actions.session_ops as so

    calls = {"kill": None, "sleep": 0, "invalidate": 0}
    probe_calls = {"n": 0}

    def probe_pid(pid, start):
        probe_calls["n"] += 1
        # call 1: pre-kill check (alive). calls 2-3: settle recheck.
        return so.proc.PidProbe(pid, probe_calls["n"] < 3)

    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(so.proc, "probe_pid", probe_pid)
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
    # Two settle polls (died on the second) — well under the ~30-poll cap —
    # proves the recheck returns as soon as death is confirmed instead of
    # always paying the full settle window.
    assert calls["sleep"] == 2
    assert probe_calls["n"] == 3
    assert calls["invalidate"] == 1


def test_take_over_survived_when_process_ignores_sigterm(monkeypatch):
    # A CLI that pops a save-confirmation modal on SIGTERM never exits: the
    # settle recheck must keep reporting it alive through the whole window
    # and the primitive must report SURVIVED (not KILLED) so callers never
    # resume/spawn a duplicate against the still-live process.
    import cc_session_control.actions.session_ops as so

    calls = {"kill": 0, "sleep": 0, "invalidate": 0}
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
        so.os, "kill", lambda *_a: calls.__setitem__("kill", calls["kill"] + 1)
    )
    monkeypatch.setattr(
        so.time, "sleep", lambda *_: calls.__setitem__("sleep", calls["sleep"] + 1)
    )
    monkeypatch.setattr(
        so,
        "invalidate_cache",
        lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1),
    )
    outcome = so.take_over_result(4242, "999")
    assert outcome.state is so.TakeOverState.SURVIVED
    assert outcome.success is False
    assert "4242" in outcome.detail
    assert "still alive" in outcome.detail
    assert calls["kill"] == 1
    # Bounded settle window, not an unbounded/one-shot wait.
    assert 0 < calls["sleep"] <= 30
    assert calls["invalidate"] == 0


def test_take_over_settle_recheck_proc_unavailable_is_refused_not_killed(monkeypatch):
    # /proc going unavailable mid-settle must not be collapsed into KILLED —
    # that would let a required takeover proceed while the target process's
    # true state is unknown.
    import cc_session_control.actions.session_ops as so

    probe_calls = {"n": 0}
    unavailable_issue = so.proc.ProcIssue(
        "process stat", "/proc/4242/stat", "unavailable"
    )

    def probe_pid(pid, start):
        probe_calls["n"] += 1
        if probe_calls["n"] == 1:
            return so.proc.PidProbe(pid, True)  # pre-kill check: alive
        return so.proc.PidProbe(pid, None, issue=unavailable_issue)

    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset({999})),
    )
    monkeypatch.setattr(so.proc, "probe_pid", probe_pid)
    monkeypatch.setattr(so.os, "kill", lambda *_a: None)
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    invalidated = {"n": 0}
    monkeypatch.setattr(
        so,
        "invalidate_cache",
        lambda: invalidated.__setitem__("n", invalidated["n"] + 1),
    )

    outcome = so.take_over_result(4242, "999")
    assert outcome.state is so.TakeOverState.REFUSED
    assert outcome.success is False
    assert "/proc/4242/stat" in outcome.detail
    assert invalidated["n"] == 0


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
    from cc_session_control.data.tmux import TmuxPane, window_containing

    panes = [TmuxPane("cc:1", 100), TmuxPane("rc:0", 200)]
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


def test_do_tmux_resume_kills_live_non_current(monkeypatch):
    import cc_session_control.actions.session_ops as so
    from cc_session_control.actions import execution_target

    calls = {"kill": [], "spawn": []}
    probe_calls = {"n": 0}
    monkeypatch.setattr(so.os, "kill", lambda pid, sig: calls["kill"].append(pid))
    monkeypatch.setattr(so.time, "sleep", lambda *_: None)
    monkeypatch.setattr(so, "invalidate_cache", lambda: None)
    monkeypatch.setattr(
        so.proc,
        "probe_current_ancestors",
        lambda: so.proc.AncestorProbe(frozenset({999})),
    )

    def probe_pid(pid, start):
        probe_calls["n"] += 1
        # call 1: pre-kill check (alive). call 2: settle recheck — dead.
        return so.proc.PidProbe(pid, probe_calls["n"] < 2)

    monkeypatch.setattr(so.proc, "probe_pid", probe_pid)
    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd, **_kwargs: (
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

    def resolve_execution_session(
        sid: str,
    ) -> execution_target.ExecutionSessionResolution:
        assert sid == s.sid
        return execution_target.ExecutionSessionResolution(
            execution_target.ExecutionSessionState.RESOLVED,
            session=s,
        )

    monkeypatch.setattr(
        execution_target,
        "resolve_execution_session",
        resolve_execution_session,
    )
    target = so.do_tmux_resume_result(s).target
    assert calls["kill"] == [4242]
    assert target == "csctl:1"  # unified workbench session, exact spawned target
    session, window, cmd = calls["spawn"][0]
    assert session == "csctl"
    assert window == "claude"  # bare CLI name — no project, no sid
    assert "--remote-control" not in cmd


def test_do_tmux_resume_dead_session_no_kill(monkeypatch):
    import cc_session_control.actions.session_ops as so

    monkeypatch.setattr(
        so.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("no kill"))
    )
    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd, **_kwargs: _created_target(
            so.tmux, f"{session}:0"
        ),
    )
    s = _make_session(sid="abcdef0123456789", cwd="/tmp/proj", alive=False)
    assert so.do_tmux_resume_result(s).target == "csctl:0"


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
        lambda session, window, cmd, **_kwargs: (
            spawns.append((session, window, cmd))
            or _created_target(so.tmux, f"{session}:0")
        ),
    )
    result = so.do_tmux_new_result("/tmp/proj with space")
    assert result.success is True
    assert result.target == "csctl:0"
    session, window, cmd = spawns[0]
    assert session == "csctl"
    assert window == "claude"  # the cwd never leaks into the window name
    assert cmd == "cd '/tmp/proj with space' && claude"
    assert "--remote-control" not in cmd and "--resume" not in cmd


def test_do_tmux_new_spawn_failure_returns_none(monkeypatch):
    import cc_session_control.actions.session_ops as so

    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda *a, **_kwargs: _create_failure(so.tmux),
    )
    result = so.do_tmux_new_result("/tmp/proj")
    assert result.success is False
    assert result.target is None


def test_do_tmux_resume_fork_spawns_fork_window_no_kill(monkeypatch):
    # A fork is a copy: never kills, and spawns its own window (named by the
    # bare CLI like every spawn — `@csctl_sid` is left EMPTY, so the fork's
    # unknown new sid is never confused with the parent's).
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
        lambda session, window, cmd, **_kwargs: (
            calls.__setitem__("tmux", (session, window, cmd))
            or _created_target(so.tmux, f"{session}:2")
        ),
    )

    s = _make_session(
        sid="abcdef0123456789", cwd="/tmp/proj", alive=True, current=False, pid=4242
    )
    assert so.do_tmux_resume_result(s, fork=True).target == "csctl:2"
    assert calls["kill"] == 0  # fork leaves the original running
    session, window, cmd = calls["tmux"]
    assert session == "csctl"
    assert window == "claude"
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
    so.do_resume_result(s)
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


def test_every_spawn_names_its_window_by_the_bare_cli(monkeypatch):
    """Resume, fork and new-session windows all carry just the CLI name —
    the project basename and sid8 prefixes are gone (2026-08-23 operator
    request); identity stays in `@csctl_sid`/`@csctl_provider`."""
    import cc_session_control.actions.session_ops as so

    windows: list[str] = []
    monkeypatch.setattr(
        so.tmux,
        "run_in_tmux_result",
        lambda session, window, cmd, **_kwargs: (
            windows.append(window) or _created_target(so.tmux, f"{session}:0")
        ),
    )
    for key in ("claude", "codex", "kimi", "opencode"):
        so.do_tmux_new_result("/tmp/my.proj", key)
    dead = _make_session(sid="abcdef0123456789", cwd="/tmp/my.proj", alive=False)
    so.do_tmux_resume_result(dead)
    so.do_tmux_resume_result(dead, fork=True)

    assert windows == ["claude", "codex", "kimi", "opencode", "claude", "claude"]


# --- D4: _parse_transcript ---


def _write_jsonl(tmp_path, sid, lines, separators=(",", ":")):
    # Compact separators by default, mirroring Claude's actual transcript
    # format. The line pre-checks in _parse_transcript are whitespace- and
    # key-order-insensitive (see test_parse_transcript_serialization_variants
    # and test_parse_transcript_key_order_reversed), so other separator
    # styles are supported too — this default just matches production.
    f = tmp_path / f"{sid}.jsonl"
    f.write_text(
        "\n".join(json.dumps(line, separators=separators) for line in lines) + "\n"
    )
    return str(f)


def _parse_transcript(path, idx, cur):
    """Test-only composition of the two production seams the removed
    `sessions._parse_transcript` compatibility wrapper used to chain: the
    record-level parser (`transcripts._parse_transcript`) projected through
    `sessions._project_transcript` (the same call `scan_result` makes per
    inventory record)."""
    transcript = transcripts_mod._parse_transcript(path)
    if transcript is None:
        return None
    return sessions_mod._project_transcript(transcript, idx, cur)


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
    s = _parse_transcript(path, idx={}, cur=set())
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
    assert _parse_transcript(path, idx={}, cur=set()) is None


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
    s = _parse_transcript(path, idx={}, cur=set())
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
    s = _parse_transcript(path, idx={}, cur=set())
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
    s = _parse_transcript(path, idx={}, cur=set())
    assert s.label == "the last prompt"


def test_parse_transcript_label_untitled(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set())
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
    s = _parse_transcript(path, idx=idx, cur={4242})
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
    s = _parse_transcript(path, idx=idx, cur={700772})
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
    s = _parse_transcript(path, idx=idx, cur=set())
    assert s.alive is True
    assert s.rc_exposed is False


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
    s = _parse_transcript(path, idx=idx, cur=set())
    assert s.rc_exposed is True


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
    s = _parse_transcript(path, idx={}, cur=set())
    assert s.hidden == {"sdk", "bridge"}


# --- B2: line pre-check must not depend on exact JSON layout ---
#
# Regression for a real incident: upstream serializers vary whitespace
# ("type":"user" vs "type": "user") and key order. The old pre-check
# `'"type":"user"' in line` only matched the first compact form, so any
# other layout silently produced prompts=0 for every session — which then
# classified real sessions as empty-shell cleanup candidates. The pre-check
# is performance-only; the actual classification always comes from the
# json.loads()'d document.


@pytest.mark.parametrize(
    "separators",
    [
        (",", ":"),  # compact, e.g. {"type":"user"}
        (",", ": "),  # colon-space, the exact upstream variant that broke this
        (", ", ":"),  # comma-space
        (", ", ": "),  # both spaces (json.dumps default)
    ],
    ids=["compact", "colon_space", "comma_space", "both_spaces"],
)
def test_parse_transcript_serialization_variants(tmp_path, separators):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"aiTitle": "The Title"},
            {"lastPrompt": "the last prompt"},
            {"type": "user", "message": {"content": "hello world"}},
        ],
        separators=separators,
    )
    s = _parse_transcript(path, idx={}, cur=set())
    assert s is not None
    assert s.cwd == "/tmp/proj"
    assert s.label == "The Title"
    assert s.prompts == 1


def test_parse_transcript_key_order_reversed(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"lastPrompt": "the last prompt", "aiTitle": "The Title"},
            {"message": {"content": "hello world"}, "type": "user"},
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set())
    assert s is not None
    assert s.cwd == "/tmp/proj"
    assert s.label == "The Title"
    assert s.prompts == 1


def test_parse_transcript_user_substring_is_not_enough_to_count(tmp_path):
    # The relaxed pre-check `'"user"' in line` is intentionally over-inclusive
    # (e.g. it also matches a line whose type is "assistant" but whose prose
    # mentions "user"). Classification must still come from json.loads(),
    # so such a line must not be miscounted as a prompt.
    path = _write_jsonl(
        tmp_path,
        "sid1",
        [
            {"cwd": "/tmp/proj"},
            {"type": "assistant", "message": {"content": "ask the user to confirm"}},
            {"type": "user", "message": {"content": "real prompt"}},
        ],
    )
    s = _parse_transcript(path, idx={}, cur=set())
    assert s is not None
    assert s.prompts == 1
