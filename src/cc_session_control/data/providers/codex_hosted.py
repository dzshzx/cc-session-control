"""Codex app-server hosted-rollout evidence.

The provider owns interpretation; `data.proc` remains the only `/proc` seam.
An exact open rollout path means "hosted, read-only", never a killable pid.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ...models import InventoryIssue
from .. import proc
from ..proc import ProcCli, ProcOpenFileInventory

_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--remote",
        "--remote-auth-token-env",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
    }
)


@dataclass(frozen=True)
class HostedRolloutScan:
    paths: frozenset[str] = frozenset()
    issues: tuple[InventoryIssue, ...] = ()


def is_app_server(record: ProcCli) -> bool:
    """Whether argv selects the real subcommand after optional global flags."""
    argv = record.argv
    if not argv or os.path.basename(argv[0]) != "codex":
        return False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "app-server":
            return True
        if not arg.startswith("-"):
            return False
        if arg in _GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
        else:
            index += 1
    return False


def scan_hosted_rollouts(
    home: Path,
    source: str,
    records: Iterable[ProcCli],
    owns_process: Callable[[ProcCli], bool],
    *,
    scan_open_files: Callable[[Iterable[int]], ProcOpenFileInventory] = (
        proc.scan_open_file_inventory
    ),
) -> HostedRolloutScan:
    """Return exact active-rollout paths held by this identity's app-server."""

    pids = tuple(
        record.pid
        for record in records
        if is_app_server(record) and owns_process(record)
    )
    if not pids:
        return HostedRolloutScan()
    inventory = scan_open_files(pids)
    root = os.path.normpath(home / "sessions") + os.sep
    paths = frozenset(
        path
        for path in inventory.paths
        if path.startswith(root) and path.endswith(".jsonl")
    )
    issues = tuple(
        InventoryIssue(source, issue.path, issue.detail) for issue in inventory.issues
    )
    return HostedRolloutScan(paths, issues)
