"""CLI wiring tests for env reporting, theme flags, rc add trust reporting,
the TUI exit-intent handoff, and `resume --take-over` safety (R7.1/R10)."""

import types
from dataclasses import replace
from pathlib import Path

import pytest

from cc_session_control import cli, cli_commands, cli_rc
from cc_session_control.actions import session_ops
from cc_session_control.config import cfg
from cc_session_control.data import liveness, sessions
from cc_session_control.data.liveness import LivenessSnapshot
from cc_session_control.models import Session


def test_env_command_reports_ledger_failure_on_stderr_and_exits_nonzero(
    tmp_path,
    monkeypatch,
    capsys,
):
    from cc_session_control.data import rc

    monkeypatch.setattr(cfg, "config_dir", tmp_path / "config")
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    cfg.config_dir.mkdir()
    path = cfg.environments_ledger
    path.write_text('{"prefix":"session","key":"OLD"}\n')
    original_open = Path.open

    def deny_ledger(target, *args, **kwargs):
        if target == path:
            raise PermissionError("history denied")
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_ledger)
    monkeypatch.setattr(rc, "scan_servers_result", lambda: rc.RCServerScanResult())
    # Hermetic liveness: CI runners have no `claude` binary, and a failed
    # `claude agents --json` scan would make every result "(partial)".
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    monkeypatch.setattr(liveness, "scan_agents", lambda *a, **k: liveness.AgentsScan())
    status = cli.main(["env"])

    captured = capsys.readouterr()
    assert status == 1
    assert "Current bridge environments: 0" in captured.out
    assert "ledger history incomplete" in captured.out
    assert (
        "Warning: environment ledger operation read failed: history denied; "
        "current rows remain visible while orphan history is incomplete"
    ) in captured.err
    assert not any("\u4e00" <= char <= "\u9fff" for char in captured.err)


def test_env_command_reports_partial_ledger_as_blocked_and_preserves_bytes(
    tmp_path,
    monkeypatch,
    capsys,
):
    from cc_session_control.data import rc

    monkeypatch.setattr(cfg, "config_dir", tmp_path / "config")
    monkeypatch.setattr(cfg, "claude_home", tmp_path / "claude")
    cfg.config_dir.mkdir()
    cfg.environments_ledger.write_text("{broken\n")
    original = cfg.environments_ledger.read_bytes()
    monkeypatch.setattr(rc, "scan_servers_result", lambda: rc.RCServerScanResult())
    monkeypatch.setattr(liveness, "alive_map", lambda *a, **k: {})
    monkeypatch.setattr(liveness, "scan_agents", lambda *a, **k: liveness.AgentsScan())

    status = cli_commands._cmd_env(types.SimpleNamespace())

    captured = capsys.readouterr()
    assert status == 1
    assert "ledger history incomplete" in captured.out
    assert (
        "Warning: environment ledger line 1 is malformed: "
        "Expecting property name enclosed in double quotes: "
        "line 1 column 2 (char 1); original ledger preserved and updates blocked; "
        "orphan history is unavailable"
    ) in captured.err
    assert not any("\u4e00" <= char <= "\u9fff" for char in captured.err)
    assert cfg.environments_ledger.read_bytes() == original


def test_theme_flag_sets_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli.build_parser().parse_args(["--theme", "light"])
    cli.apply_global_flags(args)
    assert cfg.theme == "light"


def test_theme_flag_absent_keeps_cfg(monkeypatch):
    monkeypatch.setattr(cfg, "theme", "auto")
    args = cli.build_parser().parse_args([])
    cli.apply_global_flags(args)
    assert cfg.theme == "auto"


def test_rc_add_reports_unavailable_trust_without_calling_it_untrusted(
    tmp_path,
    monkeypatch,
    capsys,
):
    from cc_session_control.data import rc

    project = tmp_path / "app"
    project.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{broken")
    monkeypatch.setattr(rc.cfg, "claude_json", claude_json)

    status = cli_rc._cmd_add(
        types.SimpleNamespace(project=str(project)),
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "Project settings unavailable" in captured.err
    assert "Not trusted" not in captured.err


def test_tui_exit_intent_runs_only_after_main_loop_returns(monkeypatch):
    from cc_session_control import app as app_mod
    from cc_session_control.actions.session_ops import ExitIntent

    events = []

    class Intent(ExitIntent):
        def run(self) -> int:
            events.append("intent")
            return 0

    intent = Intent()

    class FakeApp:
        def run(self):
            events.append("loop")
            return intent

    monkeypatch.setattr(app_mod, "App", FakeApp)

    assert cli_commands._cmd_tui(types.SimpleNamespace()) == 0

    assert events == ["loop", "intent"]


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
