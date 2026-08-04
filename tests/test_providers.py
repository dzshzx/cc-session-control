"""Provider registry + claude provider contract (ADR-0005)."""

from __future__ import annotations

import pytest

from cc_session_control import providers
from cc_session_control.actions import session_ops
from cc_session_control.config import cfg
from cc_session_control.models import Session
from cc_session_control.providers.base import LivenessGrade


def _session(**overrides):
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


class TestRegistry:
    def test_get_claude(self):
        p = providers.get("claude")
        assert p.key == "claude"
        assert p.caps.liveness is LivenessGrade.FULL

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError):
            providers.get("aider")

    def test_active_requires_allowlist(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg, "claude_home", tmp_path)
        monkeypatch.setattr(cfg, "providers", ("codex",))
        assert all(p.key != "claude" for p in providers.active_providers())

    def test_active_requires_home_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg, "providers", ("claude",))
        monkeypatch.setattr(cfg, "claude_home", tmp_path / "absent")
        assert providers.active_providers() == ()
        monkeypatch.setattr(cfg, "claude_home", tmp_path)
        assert [p.key for p in providers.active_providers()] == ["claude"]


class TestClaudeProvider:
    def test_resume_argv(self):
        p = providers.get("claude")
        assert p.resume_argv("sid-1") == ["claude", "--resume", "sid-1"]
        assert p.resume_argv("sid-1", fork=True) == [
            "claude",
            "--resume",
            "sid-1",
            "--fork-session",
        ]

    def test_new_session_argv(self):
        assert providers.get("claude").new_session_argv() == ["claude"]

    def test_window_name_keeps_bare_sid8(self):
        p = providers.get("claude")
        assert p.window_name("abcdefgh-rest") == "abcdefgh"
        assert p.window_name("abcdefgh-rest", fork=True) == "abcdefgh-fork"

    def test_caps(self):
        caps = providers.get("claude").caps
        assert caps.fork and caps.takeover and caps.cleanup
        assert caps.background_agents and caps.remote_control


class TestActionDispatch:
    def test_resume_plan_routes_through_provider(self):
        s = _session()
        cwd, args, should_kill = session_ops._resume_plan(s)
        assert cwd == s.cwd
        assert args == ["claude", "--resume", s.sid]
        assert should_kill is False

    def test_unknown_provider_session_is_loud(self):
        s = _session(provider="mystery")
        with pytest.raises(KeyError):
            session_ops._resume_plan(s)

    def test_live_non_claude_execution_fails_closed(self):
        s = _session(provider="codex", alive=True, pid=4242)
        resolution = session_ops._session_for_execution(s, fork=False)
        assert not resolution.success
        assert "codex" in resolution.detail

    def test_dead_non_claude_execution_resolves_as_is(self):
        s = _session(provider="codex", alive=False)
        resolution = session_ops._session_for_execution(s, fork=False)
        assert resolution.success
        assert resolution.session is s
