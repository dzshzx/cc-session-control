"""Cross-provider assembly pipeline (ADR-0005) — candidate B9b.

These pin the seams that glue per-provider rows into one coherent world:
`providers.merge_sessions` ordering, `snapshot._inject_provider_residency`
(the tmux evidence source for the Enter/⧉ attach path), the degraded-source
presentation surfaces (TUI status line + headless `csctl resume` warning),
and `refresh.build_refresh_result`'s exclusion of non-Claude rows from the
Claude-only cleanup universe. Regressions here would be silent: a broken
merge/residency/exclusion step does not raise, it just produces the wrong
session list, the wrong ⧉ badge, or an orphan-cleanup false positive.
"""

from __future__ import annotations

import json

from factories import make_session
from view_helpers import FakeApp, _refresh_batch

from cc_session_control import cli
from cc_session_control.config import cfg
from cc_session_control.data import liveness, providers, sessions, snapshot
from cc_session_control.data.cleanup import CleanupPlan
from cc_session_control.data.providers.kimi import KimiProvider
from cc_session_control.data.refresh import RefreshBatch, build_refresh_result
from cc_session_control.data.snapshot import WorldSnapshot
from cc_session_control.data.tmux_outcomes import ResidencyInventory, ResidencyIssue
from cc_session_control.models import InventoryIssue
from cc_session_control.views.sessions import SessionsView


class TestMergeSessionsOrdering:
    """`providers.merge_sessions` — the single cross-provider sort (mtime desc)."""

    def test_sorts_multi_provider_rows_newest_first(self):
        claude_row = make_session(sid="c1", provider="claude", mtime=300.0)
        codex_row = make_session(sid="x1", provider="codex", mtime=200.0)
        kimi_row = make_session(sid="k1", provider="kimi", mtime=100.0)

        merged = providers.merge_sessions((claude_row,), (codex_row, kimi_row))

        assert [s.sid for s in merged] == ["c1", "x1", "k1"]

    def test_kimi_row_with_missing_state_json_sinks_to_bottom_without_raising(
        self,
        tmp_path,
        monkeypatch,
    ):
        """A real degraded kimi row (state.json absent → mtime=0.0, per
        kimi.py's `_project`) must merge cleanly to the bottom, not explode
        the sort or get dropped."""
        home = tmp_path / "kimi"
        home.mkdir()
        monkeypatch.setattr(cfg, "kimi_home", home)
        sid = "session_deadbeef"
        session_dir = home / "sessions" / "wd_x" / sid
        session_dir.mkdir(parents=True)
        # No state.json written — index-only entry, the exact degradation path.
        with open(home / "session_index.jsonl", "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": sid,
                        "sessionDir": str(session_dir),
                        "workDir": "/tmp/x",
                    }
                )
                + "\n"
            )
        from cc_session_control.data.proc import ProcCliInventory

        scan = KimiProvider().discover(ProcCliInventory(), cur=frozenset())
        (kimi_row,) = scan.sessions
        assert kimi_row.mtime == 0.0  # confirms the degraded-mtime precondition

        claude_row = make_session(sid="c1", provider="claude", mtime=50.0)
        merged = providers.merge_sessions((claude_row,), (kimi_row,))

        assert [s.sid for s in merged] == ["c1", sid]

    def test_empty_groups_are_a_no_op(self):
        claude_row = make_session(sid="c1", provider="claude", mtime=1.0)
        assert providers.merge_sessions((claude_row,), ()) == (claude_row,)
        assert providers.merge_sessions((), ()) == ()


class TestInjectProviderResidency:
    """`snapshot._inject_provider_residency` — the tmux evidence source for
    non-Claude Enter/⧉ attach; Claude rows get theirs from a different path
    (sessions.scan_result's own inventory), this seam only ever touches
    non-Claude rows the caller hands it."""

    def test_live_row_gets_tmux_target_injected(self, monkeypatch):
        row = make_session(sid="x1", provider="codex", pid=42, alive=True)
        seen_pids: list[frozenset[int]] = []

        def fake_residency(pids):
            seen_pids.append(frozenset(pids))
            return ResidencyInventory(targets={42: "proj:3"})

        monkeypatch.setattr(snapshot.tmux, "residency_inventory", fake_residency)

        (result,) = snapshot._inject_provider_residency((row,))

        assert seen_pids == [frozenset({42})]
        assert result.tmux_target == "proj:3"
        assert result.tmux_inventory_complete
        assert result.tmux_inventory_detail == ""

    def test_dead_row_is_not_injected_and_never_probed(self, monkeypatch):
        row = make_session(sid="x2", provider="codex", pid=None, alive=False)

        def unexpected_call(_pids):
            raise AssertionError("a dead row has no live pid to probe residency for")

        monkeypatch.setattr(snapshot.tmux, "residency_inventory", unexpected_call)

        (result,) = snapshot._inject_provider_residency((row,))

        assert result is row
        assert result.tmux_target is None

    def test_incomplete_inventory_propagates_completeness_and_detail(
        self,
        monkeypatch,
    ):
        row = make_session(sid="x3", provider="codex", pid=99, alive=True)
        issue = ResidencyIssue("tmux list-panes", None, "tmux not running")

        def fake_residency(_pids):
            return ResidencyInventory(targets={}, issues=(issue,))

        monkeypatch.setattr(snapshot.tmux, "residency_inventory", fake_residency)

        (result,) = snapshot._inject_provider_residency((row,))

        # No target known — evidence is incomplete, not proof of "bare".
        assert result.tmux_target is None
        assert not result.tmux_inventory_complete
        assert "tmux not running" in result.tmux_inventory_detail

    def test_mixed_live_and_dead_rows_only_probe_the_live_pids(self, monkeypatch):
        live = make_session(sid="x4", provider="codex", pid=7, alive=True)
        dead = make_session(sid="x5", provider="codex", pid=None, alive=False)
        seen_pids: list[frozenset[int]] = []

        def fake_residency(pids):
            seen_pids.append(frozenset(pids))
            return ResidencyInventory(targets={7: "proj:1"})

        monkeypatch.setattr(snapshot.tmux, "residency_inventory", fake_residency)

        live_result, dead_result = snapshot._inject_provider_residency((live, dead))

        assert seen_pids == [frozenset({7})]
        assert live_result.tmux_target == "proj:1"
        assert dead_result is dead

    def test_merged_sessions_injects_residency_then_sorts_across_providers(
        self,
        monkeypatch,
    ):
        """`_merged_sessions` is the actual glue `build_world_snapshot` calls:
        residency injection must run BEFORE the merge sort, and a pure-Claude
        generation (no provider rows) must skip the tmux call entirely."""
        claude_row = make_session(sid="c1", provider="claude", mtime=10.0)
        codex_row = make_session(
            sid="x1", provider="codex", pid=42, alive=True, mtime=20.0
        )

        def fake_residency(pids):
            return ResidencyInventory(targets={42: "proj:9"})

        monkeypatch.setattr(snapshot.tmux, "residency_inventory", fake_residency)

        merged = snapshot._merged_sessions((claude_row,), (codex_row,))

        assert [s.sid for s in merged] == ["x1", "c1"]  # mtime desc
        (codex_result,) = [s for s in merged if s.sid == "x1"]
        assert codex_result.tmux_target == "proj:9"

    def test_merged_sessions_skips_residency_call_for_pure_claude_generation(
        self,
        monkeypatch,
    ):
        claude_row = make_session(sid="c1", provider="claude")

        def unexpected_call(_pids):
            raise AssertionError("no provider rows — nothing to probe residency for")

        monkeypatch.setattr(snapshot.tmux, "residency_inventory", unexpected_call)

        claude_rows = (claude_row,)
        merged = snapshot._merged_sessions(claude_rows, ())

        # "pure-Claude generation flows through uncopied" (snapshot.py comment).
        assert merged is claude_rows


class TestProviderIssuesDegradedPresentation:
    """A broken non-Claude source degrades to a visible warning — it must
    never blank the Claude rows (ADR-0005's core promise)."""

    def test_sessions_view_status_shows_degraded_source_count_and_detail(self):
        app = FakeApp()
        view = SessionsView(app)
        app.views = [view]
        claude_row = make_session(sid="c1", provider="claude", label="still here")
        issue = InventoryIssue(
            "codex session index",
            "/home/x/.codex/sessions",
            "permission denied",
        )
        batch = _refresh_batch(
            WorldSnapshot(sessions=[claude_row], provider_issues=(issue,))
        )

        view.apply_refresh(batch)

        status_text = view.status.original_widget.get_text()[0]
        assert "外部源降级 1" in status_text
        assert "permission denied" in status_text
        # The reverse invariant: a degraded non-Claude source narrows nothing
        # about the Claude rows already scanned.
        assert [s.sid for s in view._all_sessions] == ["c1"]

    def test_sessions_view_status_has_no_degraded_marker_when_issues_are_empty(self):
        app = FakeApp()
        view = SessionsView(app)
        app.views = [view]
        batch = _refresh_batch(WorldSnapshot(sessions=[make_session(sid="c1")]))

        view.apply_refresh(batch)

        assert "外部源降级" not in view.status.original_widget.get_text()[0]

    def test_cli_resume_warns_on_partial_provider_inventory_but_keeps_claude_rows(
        self,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        monkeypatch.setattr(
            liveness,
            "liveness_inputs",
            lambda: liveness.LivenessSnapshot(),
        )
        claude_row = make_session(sid="sid-claude", label="claude row")
        monkeypatch.setattr(
            sessions,
            "scan_result",
            lambda _inputs: sessions.SessionScanResult((claude_row,)),
        )
        issue = InventoryIssue(
            "codex session index",
            "/home/x/.codex/sessions",
            "permission denied",
        )
        monkeypatch.setattr(providers, "scan_non_claude", lambda cur: ((), (issue,)))

        assert cli.main(["resume"]) == 0

        captured = capsys.readouterr()
        assert "Warning: provider inventory is partial" in captured.err
        assert "permission denied" in captured.err
        assert "sid-claude" in captured.out

    def test_cli_resume_prints_no_warning_when_provider_inventory_is_complete(
        self,
        monkeypatch,
        capsys,
    ):
        monkeypatch.setattr(cfg, "providers", ("claude", "codex"))
        monkeypatch.setattr(
            liveness,
            "liveness_inputs",
            lambda: liveness.LivenessSnapshot(),
        )
        monkeypatch.setattr(
            sessions,
            "scan_result",
            lambda _inputs: sessions.SessionScanResult(
                (make_session(sid="sid-claude", label="claude row"),)
            ),
        )
        monkeypatch.setattr(providers, "scan_non_claude", lambda cur: ((), ()))

        assert cli.main(["resume"]) == 0

        captured = capsys.readouterr()
        assert "Warning: provider inventory is partial" not in captured.err


class TestRefreshExcludesProviderRowsFromCleanupUniverse:
    """`build_refresh_result` must feed the cleanup planner Claude rows only
    (`data/refresh.py`): a codex/kimi sid is not a `~/.claude` artifact sid,
    so letting it into the session-keyed universe would make the planner
    treat it as an orphan candidate."""

    def test_non_claude_rows_are_excluded_from_the_cleanup_builder_input(self):
        claude_row = make_session(sid="c1", provider="claude")
        codex_row = make_session(sid="x1", provider="codex", alive=True, pid=5)
        kimi_row = make_session(sid="k1", provider="kimi")
        snapshot_ = WorldSnapshot(
            sessions=(claude_row, codex_row, kimi_row),
            liveness_snapshot=liveness.LivenessSnapshot(),
        )
        seen: list[tuple[str, ...]] = []

        def make_plan(rows, _evidence, *, transcript_sids, age_plan):
            seen.append(tuple(r.sid for r in rows))
            return CleanupPlan()

        result = build_refresh_result(
            1,
            snapshot_builder=lambda: snapshot_,
            cleanup_builder=make_plan,
        )

        assert isinstance(result, RefreshBatch)
        assert seen == [("c1",)]
        # The full merged list still reaches the view layer unfiltered —
        # only the cleanup builder's input is narrowed.
        assert [s.sid for s in result.snapshot.sessions] == ["c1", "x1", "k1"]

    def test_pure_claude_generation_passes_the_tuple_through_uncopied(self):
        """The common case (no non-Claude rows at all) must not pay a copy —
        `refresh.py`'s own comment promises this; pin it structurally."""
        claude_row = make_session(sid="c1", provider="claude")
        rows = (claude_row,)
        snapshot_ = WorldSnapshot(
            sessions=rows,
            liveness_snapshot=liveness.LivenessSnapshot(),
        )
        seen: list[object] = []

        def make_plan(passed_rows, _evidence, *, transcript_sids, age_plan):
            seen.append(passed_rows)
            return CleanupPlan()

        build_refresh_result(
            1,
            snapshot_builder=lambda: snapshot_,
            cleanup_builder=make_plan,
        )

        assert seen[0] is snapshot_.sessions
