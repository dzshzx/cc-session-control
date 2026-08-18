"""opencode delegated official delete (`opencode session delete`, 1.18.15)."""

from __future__ import annotations

import subprocess

from cc_session_control.data import providers
from cc_session_control.data.providers import opencode as opencode_mod
from cc_session_control.data.providers.opencode import OpencodeProvider

SES1 = "ses_1aaaaaaaaaaaaaaaaaaa"


def _fake_run_factory(monkeypatch, returncode=0, stdout="", stderr=""):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(opencode_mod.subprocess, "run", fake_run)
    return calls


class TestOpencodeDelete:
    def test_delete_argv_is_the_official_verb(self):
        p = OpencodeProvider()
        assert p.delete_argv(SES1) == ["opencode", "session", "delete", SES1]

    def test_success_runs_bounded_list_argv_without_a_shell(self, monkeypatch):
        calls = _fake_run_factory(monkeypatch)
        result = OpencodeProvider().delete_session_result(SES1)
        assert result.state is providers.CliDeleteState.DELETED
        assert result.stage is providers.CliDeleteStage.CLI
        ((argv, kwargs),) = calls
        assert argv == ["opencode", "session", "delete", SES1]
        assert kwargs.get("shell") is not True
        assert kwargs["timeout"] == opencode_mod._DELETE_TIMEOUT_SECONDS

    def test_nonzero_exit_keeps_stage_code_and_stderr_tail(self, monkeypatch):
        _fake_run_factory(
            monkeypatch,
            returncode=1,
            stderr=f"Error: Session not found: {SES1}\n",
        )
        result = OpencodeProvider().delete_session_result(SES1)
        assert result.state is providers.CliDeleteState.FAILED
        assert result.stage is providers.CliDeleteStage.CLI
        assert result.returncode == 1
        assert "Session not found" in result.detail

    def test_timeout_is_a_typed_invoke_failure(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["opencode"], 10)

        monkeypatch.setattr(opencode_mod.subprocess, "run", boom)
        result = OpencodeProvider().delete_session_result(SES1)
        assert result.state is providers.CliDeleteState.FAILED
        assert result.stage is providers.CliDeleteStage.INVOKE

    def test_missing_binary_is_a_typed_invoke_failure(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise FileNotFoundError("opencode")

        monkeypatch.setattr(opencode_mod.subprocess, "run", boom)
        result = OpencodeProvider().delete_session_result(SES1)
        assert result.state is providers.CliDeleteState.FAILED
        assert result.stage is providers.CliDeleteStage.INVOKE

    def test_registry_routes_opencode_delete_through_execute_cli_delete(
        self, monkeypatch
    ):
        """A dead opencode row's Sessions `d` reaches the official verb via
        the fresh-evidence chain (providers.execute_cli_delete)."""
        provider = providers.get("opencode")
        monkeypatch.setattr(
            providers.proc,
            "probe_current_ancestors",
            lambda: providers.proc.AncestorProbe(frozenset()),
        )
        monkeypatch.setattr(
            providers.proc,
            "scan_cli_argv_inventory",
            lambda *_args: providers.proc.ProcCliInventory(),
        )
        from factories import make_session

        dead = make_session(provider="opencode", sid=SES1, alive=False)
        monkeypatch.setattr(
            provider,
            "discover",
            lambda *_args: providers.ProviderScan(sessions=(dead,)),
        )
        calls = _fake_run_factory(monkeypatch)

        result = providers.execute_cli_delete("opencode", SES1)

        assert result.success
        (call,) = calls
        assert call[0][:3] == ["opencode", "session", "delete"]
