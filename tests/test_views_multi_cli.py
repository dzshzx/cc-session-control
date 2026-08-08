"""Multi-CLI workbench surface (ADR-0005): launcher keys, badges, gates."""

from __future__ import annotations

import urwid
from view_helpers import FakeApp, _apply_projects, _make_project

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


class TestLauncherChooser:
    """Projects-tab Enter = CLI 选择器 (user-requested ADR-0005 amendment,
    2026-08-04): arrows + Enter pick one ACTIVE provider; x/k stay direct."""

    def _open(self, monkeypatch, *keys: str):
        _activate(monkeypatch, *keys)
        app = FakeApp()
        view = RCView(app)
        app.views = [view]
        _apply_projects(view, [_make_project(name="p1", directory="/tmp/p1")])
        view.handle_key("enter")
        return app, view

    def test_enter_opens_chooser_with_claude_focused(self, monkeypatch):
        app, view = self._open(monkeypatch, "claude", "codex", "kimi")
        assert app.result is None  # nothing launched yet
        assert isinstance(view._body.original_widget, urwid.Overlay)
        assert [row.provider_key for row in view._chooser_walker] == [
            "claude",
            "codex",
            "kimi",
        ]
        focused = view._chooser_walker.get_focus()[0]
        assert focused.provider_key == "claude"  # Enter-Enter ≡ old Enter

    def test_enter_enter_launches_claude_like_old_enter(self, monkeypatch):
        app, view = self._open(monkeypatch, "claude", "codex", "kimi")
        view.handle_key("enter")  # confirm the default claude row
        assert app.result == TmuxNewIntent("/tmp/p1")
        assert app.result.provider == "claude"
        assert view._body.original_widget is view._list_body  # chooser closed
        assert view._chooser is None

    def _pick(self, view, provider_key: str) -> None:
        rows = [row.provider_key for row in view._chooser_walker]
        view._chooser_walker.set_focus(rows.index(provider_key))
        view.handle_key("enter")

    def test_choosing_codex_row_launches_codex(self, monkeypatch):
        app, view = self._open(monkeypatch, "claude", "codex", "kimi")
        self._pick(view, "codex")
        assert app.result == TmuxNewIntent("/tmp/p1", provider="codex")

    def test_choosing_kimi_row_launches_kimi(self, monkeypatch):
        app, view = self._open(monkeypatch, "claude", "codex", "kimi")
        self._pick(view, "kimi")
        assert app.result == TmuxNewIntent("/tmp/p1", provider="kimi")

    def test_esc_cancels_chooser_without_intent(self, monkeypatch):
        app, view = self._open(monkeypatch, "claude", "codex")
        view.handle_key("esc")
        assert app.result is None
        assert view._body.original_widget is view._list_body
        assert view._chooser is None
        view.handle_key("x")  # the list is actionable again — direct shortcut
        assert app.result == TmuxNewIntent("/tmp/p1", provider="codex")

    def test_inactive_providers_absent_from_chooser(self, monkeypatch):
        _, view = self._open(monkeypatch, "claude", "kimi")
        assert [row.provider_key for row in view._chooser_walker] == [
            "claude",
            "kimi",  # codex inactive -> not offered
        ]

    def test_no_active_provider_refuses_chooser(self, monkeypatch):
        app, view = self._open(monkeypatch)
        assert app.result is None
        assert not isinstance(view._body.original_widget, urwid.Overlay)
        assert any("无法新建会话" in n for n in app._notifications)

    def test_x_k_direct_shortcuts_skip_chooser(self, monkeypatch):
        _activate(monkeypatch, "claude", "codex", "kimi")
        for key, provider in (("x", "codex"), ("k", "kimi")):
            app = FakeApp()
            view = RCView(app)
            app.views = [view]
            _apply_projects(view, [_make_project(name="p1", directory="/tmp/p1")])
            view.handle_key(key)
            assert not isinstance(view._body.original_widget, urwid.Overlay)
            assert app.result == TmuxNewIntent("/tmp/p1", provider=provider)

    def test_footer_and_help_carry_chooser_semantics(self, monkeypatch):
        from cc_session_control.views._keytable import help_lines

        _, view = self._open(monkeypatch, "claude")
        hints = view.keyhints()  # chooser mode has its own footer
        assert "选择 CLI" in hints
        assert "Esc 取消" in hints
        view.handle_key("esc")
        assert "Enter 新建会话" in view.keyhints()  # list-mode label unchanged
        blob = "\n".join(help_lines(view.KEY_TABLE, view.HELP_LAYOUT))
        assert "选择器" in blob  # help screen describes the chooser
        assert "直达" in blob  # …and x/k as direct shortcuts


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
        ((tmux_session, window, cmd),) = calls
        assert tmux_session == "csctl"
        assert window == "proj/codex"
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
