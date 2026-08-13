"""Sessions `d` delegates dead codex rows to the official `codex delete` (B8).

The interaction contract mirrors the Claude `d` contract exactly: single key
(no confirm modal), off-loop R10 ancestor probe at the view, execution-time
fresh-evidence revalidation in the data layer, Chinese notice + refresh.
The only difference is the executor: instead of csctl's own removal seam the
owning CLI runs its official `codex delete <SESSION>` ("Permanently delete a
saved session by id or session name" — `codex delete --help`, 0.146.0).
`cleanup.remove_session` keeps refusing non-Claude rows — the delegation is
a bypass BESIDE that boundary, never a relaxation of it. kimi 0.31.1 has no
delete subcommand, so its refusal stays and now names the upstream gap.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from factories import make_session
from view_helpers import FakeApp, _set_proc_complete

from cc_session_control.actions import tui_actions
from cc_session_control.actions.feedback import format_cli_delete_notice
from cc_session_control.config import cfg
from cc_session_control.data import proc, providers
from cc_session_control.data.providers import codex as codex_mod
from cc_session_control.data.providers.base import (
    CliDeleteResult,
    CliDeleteStage,
    CliDeleteState,
)
from cc_session_control.data.providers.codex import CodexProvider
from cc_session_control.data.tmux_outcomes import PaneInventory
from cc_session_control.models import InventoryIssue
from cc_session_control.views.sessions import SessionsView

UUID1 = "019fc784-c365-70e0-af94-a6a0b15f05b8"
DEGRADED = "liveness 降级：破坏性操作已禁用"


def _codex_session(**overrides):
    base = dict(provider="codex", sid=UUID1, label="旧任务", alive=False)
    base.update(overrides)
    return make_session(**base)


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setattr(cfg, "codex_home", home)
    return home


def _meta_line(sid: str) -> str:
    payload = {
        "id": sid,
        "session_id": sid,
        "cwd": "/tmp/proj",
        "thread_source": "user",
    }
    record = {"timestamp": "t", "type": "session_meta", "payload": payload}
    return json.dumps(record) + "\n"


def _write_active(home, sid: str) -> None:
    directory = home / "sessions" / "2026" / "08" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"rollout-a-{sid}.jsonl").write_text(_meta_line(sid))


def _write_archived(home, sid: str) -> None:
    directory = home / "archived_sessions"
    directory.mkdir(exist_ok=True)
    (directory / f"rollout-b-{sid}.jsonl").write_text(_meta_line(sid))


def _stub_evidence(
    monkeypatch,
    *,
    ancestors: frozenset[int] = frozenset({999}),
    inventory: proc.ProcCliInventory | None = None,
) -> None:
    """Fresh execution-time evidence: complete ancestors + argv inventory."""
    monkeypatch.setattr(
        proc,
        "probe_current_ancestors",
        lambda: proc.AncestorProbe(ancestors),
    )
    monkeypatch.setattr(
        proc,
        "scan_cli_argv_inventory",
        lambda basenames, env_keys=frozenset(): (
            inventory if inventory is not None else proc.ProcCliInventory()
        ),
    )
    # Keep the shared pane-evidence fetch off the real tmux (C1): an empty
    # inventory simply yields no metadata bindings.
    monkeypatch.setattr(
        providers.tmux,
        "list_panes_inventory",
        lambda: PaneInventory(),
    )


def _stub_run(monkeypatch, *, returncode=0, stdout="", stderr="", exc=None):
    """Spy on the bounded `codex delete` invocation — never runs codex."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    return calls


def _forbid_run(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("codex delete must not run past a refused gate")

    monkeypatch.setattr(codex_mod.subprocess, "run", boom)


def _live_inventory(pid: int = 4242) -> proc.ProcCliInventory:
    record = proc.ProcCli(
        pid=pid,
        argv=("codex", "resume", UUID1),
        starttime="777",
        cwd="/tmp/proj",
    )
    return proc.ProcCliInventory(records=(record,))


class TestOfficialDeleteExecutor:
    def test_delete_argv_is_the_official_verb(self):
        assert CodexProvider().delete_argv(UUID1) == ["codex", "delete", UUID1]

    def test_success_runs_bounded_list_argv_without_a_shell(self, monkeypatch):
        calls = _stub_run(monkeypatch, returncode=0)
        result = CodexProvider().delete_session_result(UUID1)
        assert result.state is CliDeleteState.DELETED
        assert result.success
        ((argv, kwargs),) = calls
        # list argv straight from the provider's own synthesis — the sid is
        # never interpolated into a shell string (no injection surface).
        assert argv == ["codex", "delete", UUID1]
        assert "shell" not in kwargs
        assert kwargs["timeout"] == codex_mod._DELETE_TIMEOUT_SECONDS

    def test_nonzero_exit_keeps_stage_code_and_stderr_tail(self, monkeypatch):
        _stub_run(monkeypatch, returncode=1, stderr="error: session not found\n")
        result = CodexProvider().delete_session_result(UUID1)
        assert result.state is CliDeleteState.FAILED
        assert result.stage is CliDeleteStage.CLI
        assert result.returncode == 1
        assert "session not found" in result.detail

    def test_long_stderr_is_truncated_to_its_tail(self, monkeypatch):
        stderr = "HEAD-marker" + "x" * 300 + "tail-marker"
        _stub_run(monkeypatch, returncode=2, stderr=stderr)
        result = CodexProvider().delete_session_result(UUID1)
        assert result.detail.endswith("tail-marker")
        assert "HEAD-marker" not in result.detail

    def test_timeout_is_a_typed_invoke_failure(self, monkeypatch):
        _stub_run(
            monkeypatch,
            exc=subprocess.TimeoutExpired(["codex", "delete", UUID1], 10),
        )
        result = CodexProvider().delete_session_result(UUID1)
        assert result.state is CliDeleteState.FAILED
        assert result.stage is CliDeleteStage.INVOKE
        assert "timed out" in result.detail

    def test_missing_binary_is_a_typed_invoke_failure(self, monkeypatch):
        _stub_run(monkeypatch, exc=FileNotFoundError("codex"))
        result = CodexProvider().delete_session_result(UUID1)
        assert result.state is CliDeleteState.FAILED
        assert result.stage is CliDeleteStage.INVOKE
        assert "not found" in result.detail


class TestExecutionProtection:
    def test_dead_row_revalidates_fresh_then_invokes_the_cli(
        self, monkeypatch, codex_home
    ):
        _write_active(codex_home, UUID1)
        _stub_evidence(monkeypatch)
        calls = _stub_run(monkeypatch, returncode=0)
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.success
        ((argv, _kwargs),) = calls
        assert argv == ["codex", "delete", UUID1]

    def test_r10_degraded_refuses_before_any_subprocess(self, monkeypatch, codex_home):
        _write_active(codex_home, UUID1)
        issue = proc.ProcIssue("process ancestors", "/proc", "unavailable")
        monkeypatch.setattr(
            proc,
            "probe_current_ancestors",
            lambda: proc.AncestorProbe(frozenset(), (issue,)),
        )
        _forbid_run(monkeypatch)
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.state is CliDeleteState.REFUSED
        assert result.stage is CliDeleteStage.EVIDENCE
        assert "ancestor evidence incomplete" in result.detail

    def test_incomplete_argv_inventory_refuses(self, monkeypatch, codex_home):
        _write_active(codex_home, UUID1)
        degraded = proc.ProcCliInventory(
            issues=(proc.ProcIssue("cli processes", "/proc", "walk failed"),),
        )
        _stub_evidence(monkeypatch, inventory=degraded)
        _forbid_run(monkeypatch)
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.state is CliDeleteState.REFUSED
        assert result.stage is CliDeleteStage.EVIDENCE
        assert "process evidence incomplete" in result.detail

    def test_incomplete_discovery_refuses(self, monkeypatch, codex_home):
        _stub_evidence(monkeypatch)
        _forbid_run(monkeypatch)
        monkeypatch.setattr(
            CodexProvider,
            "discover",
            lambda self, inventory, cur, panes=None: providers.ProviderScan(
                issues=(InventoryIssue("codex sessions", "/x", "unreadable"),),
            ),
        )
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.state is CliDeleteState.REFUSED
        assert result.stage is CliDeleteStage.EVIDENCE
        assert "session discovery incomplete" in result.detail

    def test_now_live_row_refuses(self, monkeypatch, codex_home):
        _write_active(codex_home, UUID1)
        _stub_evidence(monkeypatch, inventory=_live_inventory())
        _forbid_run(monkeypatch)
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.state is CliDeleteState.REFUSED
        assert result.stage is CliDeleteStage.PROTECTION
        assert "live" in result.detail

    def test_current_row_refuses(self, monkeypatch, codex_home):
        _write_active(codex_home, UUID1)
        _stub_evidence(
            monkeypatch,
            ancestors=frozenset({4242}),
            inventory=_live_inventory(pid=4242),
        )
        _forbid_run(monkeypatch)
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.state is CliDeleteState.REFUSED
        assert result.stage is CliDeleteStage.PROTECTION
        assert "current session" in result.detail

    def test_archived_row_refuses(self, monkeypatch, codex_home):
        _write_archived(codex_home, UUID1)
        _stub_evidence(monkeypatch)
        _forbid_run(monkeypatch)
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.state is CliDeleteState.REFUSED
        assert result.stage is CliDeleteStage.PROTECTION
        assert "archived" in result.detail

    def test_missing_row_refuses(self, monkeypatch, codex_home):
        _stub_evidence(monkeypatch)
        _forbid_run(monkeypatch)
        result = providers.execute_cli_delete("codex", UUID1)
        assert result.state is CliDeleteState.REFUSED
        assert result.stage is CliDeleteStage.PROTECTION
        assert "not found in fresh discovery" in result.detail

    @pytest.mark.parametrize("key", ["claude", "kimi"])
    def test_registry_dispatch_is_loud_without_delete_verbs(self, key):
        with pytest.raises(TypeError, match="delete verbs"):
            providers.execute_cli_delete(key, UUID1)


class TestDeleteNotices:
    def test_success_notice_mirrors_claude(self):
        result = CliDeleteResult(CliDeleteState.DELETED, CliDeleteStage.CLI)
        assert format_cli_delete_notice(result) == "已删除"

    def test_evidence_refusal_mirrors_claude_protection_wording(self):
        result = CliDeleteResult(
            CliDeleteState.REFUSED,
            CliDeleteStage.EVIDENCE,
            "ancestor evidence incomplete: /proc unavailable",
        )
        notice = format_cli_delete_notice(result)
        assert notice.startswith("保护证据不完整，未删除：")
        assert "/proc unavailable" in notice

    def test_protection_refusal_reads_as_not_deleted(self):
        result = CliDeleteResult(
            CliDeleteState.REFUSED,
            CliDeleteStage.PROTECTION,
            f"session '{UUID1}' is live; stop it first",
        )
        assert format_cli_delete_notice(result).startswith("未删除：")

    def test_cli_failure_notice_names_stage_and_stderr(self):
        result = CliDeleteResult(
            CliDeleteState.FAILED,
            CliDeleteStage.CLI,
            "codex delete exited with status 1: error: session not found",
            returncode=1,
        )
        notice = format_cli_delete_notice(result)
        assert notice.startswith("删除失败（cli）：")
        assert "session not found" in notice

    def test_invoke_failure_notice_names_stage(self):
        result = CliDeleteResult(
            CliDeleteState.FAILED,
            CliDeleteStage.INVOKE,
            "codex executable not found on PATH",
        )
        assert format_cli_delete_notice(result).startswith("删除失败（invoke）：")

    def test_worker_adapter_wraps_result_and_requests_refresh(self, monkeypatch):
        monkeypatch.setattr(
            tui_actions.providers,
            "execute_cli_delete",
            lambda key, sid: CliDeleteResult(
                CliDeleteState.DELETED, CliDeleteStage.CLI
            ),
        )
        result = tui_actions.delete_session(_codex_session())
        assert result.message == "已删除"
        assert result.needs_refresh

    def test_worker_adapter_keeps_claude_on_the_csctl_removal_seam(self, monkeypatch):
        seen: list[str] = []

        def fake_remove(session):
            seen.append(session.sid)
            from cc_session_control.data.removal import CleanupExecution

            return CleanupExecution(completed=[session.sid])

        monkeypatch.setattr(tui_actions.cleanup, "remove_session", fake_remove)
        monkeypatch.setattr(
            tui_actions.providers,
            "execute_cli_delete",
            lambda key, sid: pytest.fail("claude must never delegate"),
        )
        result = tui_actions.delete_session(make_session(sid="c1"))
        assert seen == ["c1"]
        assert result.message == "已删除"


class TestViewKeyD:
    def _view(self, session):
        app = FakeApp()
        view = SessionsView(app)
        app.views = [view]
        view._sessions = [session]
        view._all_sessions = [session]
        view._rebuild()
        return app, view

    def test_dead_codex_d_mirrors_the_claude_contract_end_to_end(
        self, monkeypatch, codex_home
    ):
        """Single key: probe → worker → notice → refresh; no confirm modal."""
        _write_active(codex_home, UUID1)
        _stub_evidence(monkeypatch)
        calls = _stub_run(monkeypatch, returncode=0)
        s = _codex_session()
        app, view = self._view(s)
        refreshed: list[bool] = []
        app.trigger_async_refresh = lambda: refreshed.append(True)

        view._key_delete(s)

        assert app._submitted_actions == ["session.delete"]
        assert app._confirm_messages == []
        assert app._notifications == ["已删除"]
        assert refreshed == [True]
        ((argv, _kwargs),) = calls
        assert argv == ["codex", "delete", UUID1]

    def test_codex_d_r10_degraded_refuses_at_the_probe(self, monkeypatch):
        _set_proc_complete(monkeypatch, proc, False)
        _forbid_run(monkeypatch)
        s = _codex_session()
        app, view = self._view(s)
        view._key_delete(s)
        assert app._notifications == [DEGRADED]
        assert app._submitted_actions == []

    def test_live_codex_d_refuses_stop_first(self, monkeypatch):
        _forbid_run(monkeypatch)
        s = _codex_session(alive=True, pid=4242)
        app, view = self._view(s)
        view._key_delete(s)
        assert app._notifications == ["运行中的会话不删，先停止"]
        assert app._submitted_actions == []

    def test_archived_codex_d_keeps_the_b7_refusal_chain(self, monkeypatch):
        _forbid_run(monkeypatch)
        s = _codex_session(archived=True)
        app, view = self._view(s)
        view._key_delete(s)
        assert app._submitted_actions == []
        assert app._confirm_messages == []
        assert app._notifications == [
            f"该会话已归档：先 codex unarchive {UUID1[:8]}… 恢复后再接回。"
        ]

    def test_kimi_d_refusal_names_the_upstream_gap(self):
        s = make_session(provider="kimi", sid="session_abc")
        app, view = self._view(s)
        view._key_delete(s)
        assert app._submitted_actions == []
        assert app._notifications == [
            "kimi 会话由其 CLI 自己管理，csctl 不删除（kimi 无官方删除命令）"
        ]

    def test_claude_d_still_routes_to_the_csctl_worker(self, monkeypatch):
        _set_proc_complete(monkeypatch, proc, True)
        seen: list[str] = []
        monkeypatch.setattr(
            tui_actions,
            "delete_session",
            lambda session: (
                seen.append(session.sid)
                or tui_actions.ActionResult("已删除", needs_refresh=True)
            ),
        )
        s = make_session(sid="c1", label="cl")
        app, view = self._view(s)
        view._key_delete(s)
        assert app._submitted_actions == ["session.delete"]
        assert seen == ["c1"]
