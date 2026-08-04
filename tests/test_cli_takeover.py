"""`csctl resume --take-over` execution-time re-resolution and safety
guarantees (R7.1/R10): never kill a recycled or current-ancestor pid,
refuse unsafe targets before touching kill/chdir/exec, and surface exec
failures with context."""

import json
from dataclasses import replace

import pytest

from cc_session_control import cli
from cc_session_control.actions import session_ops
from cc_session_control.config import cfg
from cc_session_control.data import liveness, sessions
from cc_session_control.data.liveness import LivenessSnapshot
from cc_session_control.models import Session


def _takeover_session(tmp_path, **changes):
    session = Session(
        sid="stable-sid",
        cwd=str(tmp_path),
        label="target",
        mtime=1,
        prompts=1,
        pid=None,
        alive=False,
        current=False,
    )
    return replace(session, **changes)


def _install_takeover_rows(monkeypatch, rows):
    monkeypatch.setattr(liveness, "liveness_inputs", lambda: LivenessSnapshot())
    monkeypatch.setattr(
        sessions,
        "scan_result",
        lambda _inputs: sessions.SessionScanResult(rows),
    )


def test_resume_take_over_re_resolves_execution_time_identity(
    tmp_path, monkeypatch, capsys
):
    displayed = _takeover_session(
        tmp_path, cwd="/display-time", pid=4242, proc_start="old", alive=True
    )
    assert session_ops.resume_cmd(displayed) == "csctl resume --take-over stable-sid"
    target = replace(displayed, cwd=str(tmp_path), pid=9002, proc_start="new-start")
    _install_takeover_rows(monkeypatch, (target,))
    monkeypatch.setattr(
        session_ops.proc,
        "probe_current_ancestors",
        lambda: session_ops.proc.AncestorProbe(frozenset({111})),
    )
    probes, killed, changed, executed = [], [], [], []
    monkeypatch.setattr(
        session_ops.proc,
        "probe_pid",
        lambda pid, start: (
            probes.append((pid, start)) or session_ops.proc.PidProbe(pid, True)
        ),
    )
    monkeypatch.setattr(session_ops.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(session_ops.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(session_ops.os, "chdir", lambda cwd: changed.append(cwd))
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda program, argv: executed.append((program, argv)),
    )

    assert cli.main(["resume", "--take-over", "stable-sid"]) == 0
    assert capsys.readouterr().err == ""
    assert probes == [(9002, "new-start")]
    assert killed == [9002]
    assert changed == [str(tmp_path)]
    assert executed == [("claude", ["claude", "--resume", "stable-sid"])]


@pytest.mark.parametrize("unsafe_state", ["recycled", "ancestor"])
def test_resume_take_over_never_kills_recycled_or_current_ancestor_pid(
    unsafe_state, tmp_path, monkeypatch, capsys
):
    target = _takeover_session(
        tmp_path, pid=9002, proc_start="expected-start", alive=True
    )
    _install_takeover_rows(monkeypatch, (target,))
    ancestors = {9002} if unsafe_state == "ancestor" else {111}
    monkeypatch.setattr(
        session_ops.proc,
        "probe_current_ancestors",
        lambda: session_ops.proc.AncestorProbe(frozenset(ancestors)),
    )
    monkeypatch.setattr(
        session_ops.proc,
        "probe_pid",
        lambda pid, _start: session_ops.proc.PidProbe(pid, False),
    )
    monkeypatch.setattr(
        session_ops.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not kill")),
    )
    executed = []
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda program, argv: executed.append((program, argv)),
    )

    status = cli.main(["resume", "--take-over", "stable-sid"])
    captured = capsys.readouterr()
    if unsafe_state == "ancestor":
        assert status == 1
        assert "current session ancestor chain" in captured.err
        assert executed == []
    else:
        assert status == 0
        assert executed == [("claude", ["claude", "--resume", "stable-sid"])]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", "missing"),
        ("ambiguous", "ambiguous"),
        ("current", "current"),
        ("pid", "pid"),
        ("proc_start", "proc_start"),
        ("cwd", "usable"),
        ("liveness", "liveness evidence is incomplete"),
        ("transcript", "transcript inventory is incomplete"),
    ],
)
def test_resume_take_over_refuses_unsafe_target_before_kill_or_exec(
    case, expected, tmp_path, monkeypatch, capsys
):
    target = _takeover_session(tmp_path)
    rows = (target,)
    if case == "missing":
        rows = ()
    elif case == "ambiguous":
        rows = (target, replace(target, cwd=str(tmp_path / "other")))
    elif case == "current":
        rows = (replace(target, alive=True, current=True, pid=9002),)
    elif case == "pid":
        rows = (replace(target, alive=True, proc_start="known"),)
    elif case == "proc_start":
        rows = (replace(target, alive=True, pid=9002),)
    elif case == "cwd":
        rows = (replace(target, cwd=str(tmp_path / "missing")),)

    if case == "liveness":
        issue = liveness.LivenessIssue("process ancestors", "/proc", "unavailable")
        monkeypatch.setattr(
            liveness,
            "liveness_inputs",
            lambda: LivenessSnapshot(issues=(issue,)),
        )
        monkeypatch.setattr(
            sessions,
            "scan_result",
            lambda _inputs: (_ for _ in ()).throw(AssertionError("must not scan")),
        )
    else:
        _install_takeover_rows(monkeypatch, rows)
        if case == "transcript":
            issue = sessions.TranscriptIssue("session transcript", "/x", "unreadable")
            monkeypatch.setattr(
                sessions,
                "scan_result",
                lambda _inputs: sessions.SessionScanResult(rows, (issue,)),
            )
    for boundary in ("kill", "chdir", "execvp"):
        monkeypatch.setattr(
            session_ops.os,
            boundary,
            lambda *_args: (_ for _ in ()).throw(AssertionError("unsafe boundary")),
        )

    assert cli.main(["resume", "--take-over", "stable-sid"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err


def test_resume_take_over_refuses_non_claude_sid_with_provider_hint(
    tmp_path, monkeypatch, capsys
):
    # `--take-over` is Claude-only (ADR-0005); a real codex/kimi sid must not
    # be misreported as "missing" — the rejection should point the operator
    # at the provider that actually owns it and its direct resume command.
    sid = "019fc784-c365-70e0-af94-a6a0b15f05b8"
    codex_home = tmp_path / "codex"
    rollout_dir = codex_home / "sessions" / "2026" / "08" / "01"
    rollout_dir.mkdir(parents=True)
    record = {
        "timestamp": "t",
        "type": "session_meta",
        "payload": {
            "id": sid,
            "session_id": sid,
            "cwd": str(tmp_path),
            "thread_source": "user",
        },
    }
    (rollout_dir / f"rollout-{sid}.jsonl").write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(cfg, "codex_home", codex_home)
    monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
    _install_takeover_rows(monkeypatch, ())  # no Claude session owns this sid

    status = cli.main(["resume", "--take-over", sid])
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out == ""
    assert "belongs to codex" in captured.err
    assert f"codex resume {sid}" in captured.err


def test_resume_take_over_missing_sid_with_no_provider_match_stays_plain(
    tmp_path, monkeypatch, capsys
):
    # Same missing-Claude-sid case as above, but no provider owns it either
    # (autouse fixture keeps codex/kimi inactive) — the detail must stay the
    # plain "missing session id" message, not a fabricated provider hint.
    _install_takeover_rows(monkeypatch, ())

    status = cli.main(["resume", "--take-over", "stable-sid"])
    captured = capsys.readouterr()

    assert status == 1
    assert "missing session id" in captured.err
    assert "belongs to" not in captured.err


def test_resume_take_over_exec_failure_is_contextual(tmp_path, monkeypatch, capsys):
    _install_takeover_rows(monkeypatch, (_takeover_session(tmp_path),))
    monkeypatch.setattr(
        session_ops.os,
        "execvp",
        lambda *_args: (_ for _ in ()).throw(OSError("exec denied")),
    )

    assert cli.main(["resume", "--take-over", "stable-sid"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to take over session stable-sid" in captured.err
    assert "exec denied" in captured.err
