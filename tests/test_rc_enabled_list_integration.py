"""Typed enabled-list integration tests for RC production entry points."""

import inspect

from cc_session_control import cli_rc
from cc_session_control.actions import tui_actions
from cc_session_control.data import rc
from cc_session_control.data.project_settings import (
    ProjectSettingsResult,
    ProjectSettingsState,
)
from cc_session_control.data.rc_enabled import (
    EnabledListOperation,
    EnabledListResult,
    EnabledListStage,
    EnabledListState,
)
from cc_session_control.data.tmux import TmuxWindow, WindowInventory


def _enabled_failure(
    operation: EnabledListOperation,
    stage: EnabledListStage,
    *,
    committed: bool = False,
) -> EnabledListResult:
    return EnabledListResult(
        operation,
        EnabledListState.FAILED,
        None,
        changed=committed,
        committed=committed,
        stage=stage,
        detail="permission denied",
    )


def test_enabled_list_failure_keeps_partial_scan_rows_and_stops_dependents(
    tmp_path,
    monkeypatch,
) -> None:
    trusted = tmp_path / "trusted"
    discovered = tmp_path / "discovered"
    trusted.mkdir()
    discovered.mkdir()
    list_failure = _enabled_failure(
        EnabledListOperation.LIST,
        EnabledListStage.READ,
    )
    remove_failure = _enabled_failure(
        EnabledListOperation.REMOVE,
        EnabledListStage.UNLOCK,
        committed=True,
    )

    class Store:
        def list_result(self) -> EnabledListResult:
            return list_failure

        def remove_result(self, _path: str) -> EnabledListResult:
            return remove_failure

        def list(self) -> list[str]:
            raise AssertionError("production used primitive list")

        def remove(self, _path: str) -> bool:
            raise AssertionError("production used primitive remove")

    monkeypatch.setattr(rc, "_enabled_store", Store)
    monkeypatch.setattr(
        rc,
        "_read_projects",
        lambda: ProjectSettingsResult(
            ProjectSettingsState.AVAILABLE,
            {str(trusted): {"hasTrustDialogAccepted": True}},
        ),
    )
    monkeypatch.setattr(rc, "_TEMP_ROOTS", frozenset())
    scan = rc.scan_result(
        window_inventory=WindowInventory(
            (TmuxWindow("@1", "discovered", False, 7, str(discovered)),)
        )
    )

    assert scan.enabled_list is list_failure
    assert scan.complete is False
    assert {project.directory for project in scan.projects} == {
        str(trusted),
        str(discovered),
    }
    assert not any(project.in_list for project in scan.projects)

    stops: list[str] = []
    starts: list[str] = []
    monkeypatch.setattr(
        rc,
        "stop_one_result",
        lambda path: stops.append(path),
    )
    monkeypatch.setattr(
        rc,
        "start_one_result",
        lambda path: starts.append(path),
    )

    removed = rc.remove_one_result(str(trusted))
    assert removed.enabled_list is remove_failure
    assert removed.stop is None
    assert stops == []

    started = rc.start_all_listed_result()
    assert started.enabled_list is list_failure
    assert started.results == ()
    assert starts == []


def test_production_enabled_list_paths_do_not_call_compatibility_primitives() -> None:
    # cli_rc has no single dispatch function post-refactor (each `rc <leaf>`
    # subcommand binds its own `_cmd_*` directly to argparse) — concatenate
    # every leaf's source so the "cli" surface still covers the whole family.
    cli_rc_leaf_sources = "\n".join(
        inspect.getsource(fn)
        for fn in (
            cli_rc._cmd_status,
            cli_rc._cmd_add,
            cli_rc._cmd_rm,
            cli_rc._cmd_up,
            cli_rc._cmd_stop,
            cli_rc._cmd_list,
        )
    )
    sources = {
        "scan": inspect.getsource(rc.scan_result),
        "remove": inspect.getsource(rc.remove_one_result),
        "start-all": inspect.getsource(rc.start_all_listed_result),
        "cli": cli_rc_leaf_sources,
        "toggle-action": inspect.getsource(tui_actions.toggle_autostart),
        "start-all-action": inspect.getsource(tui_actions.start_all_projects),
    }
    forbidden = {
        "scan": ("list_enabled(",),
        "remove": ("list_rm(",),
        "start-all": ("list_enabled(",),
        "cli": ("rc.list_enabled(", "rc.list_add(", "rc.list_rm("),
        "toggle-action": ("rc.toggle_autostart(",),
        "start-all-action": (),
    }

    for surface, needles in forbidden.items():
        for needle in needles:
            assert needle not in sources[surface], (surface, needle)
    for surface in ("cli", "toggle-action", "start-all-action"):
        assert "except (OSError, UnicodeError)" not in sources[surface]
