"""Multi-CLI workbench surface (ADR-0005): launcher keys, badges, gates."""

from __future__ import annotations

from view_helpers import FakeApp, _make_project

from cc_session_control.actions import session_ops
from cc_session_control.actions.resume_list import format_session
from cc_session_control.actions.session_ops import TmuxNewIntent
from cc_session_control.data import providers, tmux
from cc_session_control.models import Session
from cc_session_control.views._session_row import SessionRow
from cc_session_control.views.rc import RCView
from cc_session_control.views.sessions import SessionsView


def _session(**overrides) -> Session:
    base = dict(
        sid="abc12345-0000-0000-0000-000000000000",
        cwd="/tmp/proj",
        label="x",
        mtime=1.0,
        prompts=3,
        pid=None,
        alive=False,
        current=False,
    )
    base.update(overrides)
    return Session(**base)


def _activate(monkeypatch, *keys: str) -> None:
    active = tuple(providers.get(k) for k in keys)
    monkeypatch.setattr(providers, "active_providers", lambda: active)


class TestLauncherKeys:
    def test_enter_still_launches_claude(self, monkeypatch):
        _activate(monkeypatch, "claude")
        app = FakeApp()
        view = RCView(app)
        view._key_tmux_new(_make_project())
        assert app.result == TmuxNewIntent("/tmp/myproj")
        assert app.result.provider == "claude"

    def test_x_launches_codex_when_active(self, monkeypatch):
        _activate(monkeypatch, "claude", "codex")
        app = FakeApp()
        view = RCView(app)
        view._key_tmux_new_codex(_make_project())
        assert app.result == TmuxNewIntent("/tmp/myproj", provider="codex")

    def test_k_refused_when_kimi_inactive(self, monkeypatch):
        _activate(monkeypatch, "claude")
        app = FakeApp()
        view = RCView(app)
        view._key_tmux_new_kimi(_make_project())
        assert app.result is None
        assert any("kimi" in n and "未启用" in n for n in app._notifications)


class TestTmuxNewDispatch:
    def test_provider_argv_and_window(self, monkeypatch):
        calls = []

        def fake_run(session_name, window, cmd, **_kwargs):
            calls.append((session_name, window, cmd))
            return tmux.TmuxWriteResult(
                stage=tmux.TmuxWriteStage.NEW_WINDOW,
                state=tmux.TmuxWriteState.SUCCEEDED,
                target="s:1",
            )

        monkeypatch.setattr(session_ops.tmux, "run_in_tmux_result", fake_run)
        result = session_ops.do_tmux_new_result("/tmp/proj", "codex")
        assert result.success
        ((_, window, cmd),) = calls
        assert window == "codex"
        assert cmd.endswith("&& codex")


class TestSessionSurface:
    def test_row_carries_provider_badge(self):
        from view_helpers import _row_text

        text = _row_text(SessionRow(_session(provider="codex")))
        assert "cx" in text
        assert "p3" not in text  # non-Claude prompt count is unknown, not 0

    def test_fork_gated_by_capability(self):
        app = FakeApp()
        view = SessionsView(app)
        view._key_fork(_session(provider="kimi"))
        assert any("不支持" in n for n in app._notifications)

    def test_resume_cmd_non_claude_live_is_direct(self):
        s = _session(provider="codex", alive=True, pid=1)
        cmd = session_ops.resume_cmd(s)
        assert "csctl" not in cmd
        assert "codex resume" in cmd

    def test_headless_line_tags_non_claude(self):
        lines = format_session(_session(provider="kimi", sid="session_x"))
        assert lines[0].count("[kimi]") == 1
        claude_lines = format_session(_session())
        assert "[claude]" not in claude_lines[0]
