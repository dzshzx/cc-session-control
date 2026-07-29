"""Shared plain helpers for the split `test_cli_entry*` modules."""

from cc_session_control.data.rc_enabled import (
    EnabledListOperation,
    EnabledListResult,
    EnabledListState,
)


def enabled_list(paths: tuple[str, ...]) -> EnabledListResult:
    return EnabledListResult(
        EnabledListOperation.LIST,
        EnabledListState.SUCCEEDED,
        paths,
        changed=False,
        committed=False,
    )
