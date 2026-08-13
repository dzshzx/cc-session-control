"""Provider registry + claude provider contract (ADR-0005)."""

from __future__ import annotations

import pytest

from cc_session_control.actions import execution_target, session_ops
from cc_session_control.config import cfg
from cc_session_control.data import providers
from cc_session_control.data.providers.base import LivenessGrade
from cc_session_control.data.providers.claude import ClaudeProvider
from cc_session_control.models import Session

# Captured at import time — BEFORE the conftest autouse fixture stubs it —
# so the home-dir activation test can exercise the real predicate.
_REAL_CLAUDE_AVAILABLE = ClaudeProvider.available


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

    def test_non_claude_liveness_includes_dispatch_metadata(self):
        assert providers.get("codex").caps.liveness is LivenessGrade.TMUX
        assert providers.get("kimi").caps.liveness is LivenessGrade.TMUX

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError):
            providers.get("aider")

    def test_active_requires_allowlist(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg, "claude_home", tmp_path)
        monkeypatch.setattr(cfg, "providers", ("codex",))
        assert all(p.key != "claude" for p in providers.active_providers())

    def test_active_requires_home_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ClaudeProvider, "available", _REAL_CLAUDE_AVAILABLE)
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

    def test_live_non_claude_execution_re_resolves_via_argv(self, monkeypatch):
        s = _session(provider="codex", alive=True, pid=4242)
        fresh = _session(provider="codex", alive=True, pid=5555, proc_start="77")
        seen: list[tuple[str, str]] = []

        def fake_resolve(provider_key, sid):
            seen.append((provider_key, sid))
            return providers.ArgvResolution(session=fresh)

        monkeypatch.setattr(
            execution_target.providers,
            "resolve_argv_execution",
            fake_resolve,
        )
        resolution = session_ops.session_for_execution(s, fork=False)
        assert seen == [("codex", s.sid)]
        assert resolution.success
        assert resolution.session is fresh

    def test_live_non_claude_execution_fails_closed_on_refusal(
        self,
        monkeypatch,
    ):
        s = _session(provider="codex", alive=True, pid=4242)
        monkeypatch.setattr(
            execution_target.providers,
            "resolve_argv_execution",
            lambda provider_key, sid: providers.ArgvResolution(detail="nope"),
        )
        resolution = session_ops.session_for_execution(s, fork=False)
        assert not resolution.success
        assert resolution.detail == "nope"

    def test_dead_non_claude_execution_resolves_as_is(self):
        s = _session(provider="codex", alive=False)
        resolution = session_ops.session_for_execution(s, fork=False)
        assert resolution.success
        assert resolution.session is s

    def test_terminal_resume_execs_the_provider_binary(
        self,
        monkeypatch,
        tmp_path,
    ):
        """Regression: `t` on a codex row must exec codex, never claude."""
        execs: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(
            session_ops.os,
            "execvp",
            lambda binary, args: execs.append((binary, args)),
        )
        s = _session(provider="codex", alive=False, cwd=str(tmp_path))
        outcome = session_ops._do_resume_resolved_result(s)
        assert outcome.success
        ((binary, args),) = execs
        assert binary == "codex"
        assert args == ["codex", "resume", s.sid]

    def test_delete_refuses_non_claude_sessions(self):
        """csctl never deletes state it does not model (ADR-0005): a codex
        row's file anchor points into ~/.codex — the data boundary refuses."""
        from cc_session_control.data import cleanup

        s = _session(provider="codex", file="/tmp/rollout.jsonl")
        execution = cleanup.remove_session(s)
        assert execution.refused
        assert any("not csctl-deletable" in r.reason for r in execution.refused)
