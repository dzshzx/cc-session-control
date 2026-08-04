"""Provider registry — key → AgentProvider (ADR-0005).

The static table below is the single provider authority. A provider is
*active* when the operator allows it (`cfg.providers`, env `CSCTL_PROVIDERS`)
AND its CLI home state exists; activation never fails the refresh — an
absent CLI simply contributes no rows.
"""

from __future__ import annotations

import os.path
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from .. import proc
from ...models import InventoryIssue, Session
from .base import (
    AgentProvider,
    DiskDiscovery,
    LivenessGrade,
    ProviderCaps,
    ProviderScan,
)
from .claude import ClaudeProvider
from .codex import CodexProvider
from .kimi import KimiProvider

__all__ = [
    "AgentProvider",
    "DiskDiscovery",
    "LivenessGrade",
    "ProviderCaps",
    "ProviderScan",
    "ArgvResolution",
    "all_providers",
    "active_providers",
    "get",
    "scan_non_claude",
    "resolve_argv_execution",
]

_ALL: tuple[AgentProvider, ...] = (ClaudeProvider(), CodexProvider(), KimiProvider())


def all_providers() -> tuple[AgentProvider, ...]:
    return _ALL


def active_providers() -> tuple[AgentProvider, ...]:
    """Allowed AND locally present, in registry order (claude first)."""
    from ...config import cfg

    return tuple(p for p in _ALL if p.key in cfg.providers and p.available())


def get(key: str) -> AgentProvider:
    """The provider owning `key`; raises KeyError for an unknown provider —
    an unknown `Session.provider` is a programming error, not a degraded
    source, so it must stay loud."""
    for p in _ALL:
        if p.key == key:
            return p
    raise KeyError(f"unknown provider {key!r}")


def scan_non_claude(
    cur: AbstractSet[int],
) -> tuple[tuple[Session, ...], tuple[InventoryIssue, ...]]:
    """Discover every active non-Claude provider's sessions in one pass.

    ONE `/proc` argv walk serves all providers (their extractors are pure);
    per-provider disk issues merge into one non-fatal issue stream — a
    degraded codex index must never blank the Claude view (ADR-0005).
    """
    discoverers = [
        p
        for p in active_providers()
        if p.key != "claude" and isinstance(p, DiskDiscovery)
    ]
    if not discoverers:
        return (), ()
    basenames = frozenset(p.basename for p in discoverers)
    inventory = proc.scan_cli_argv_inventory(basenames)
    rows: list[Session] = []
    issues: list[InventoryIssue] = list(inventory.issues)
    for p in discoverers:
        scan = p.discover(inventory, cur)
        rows.extend(scan.sessions)
        issues.extend(scan.issues)
    return tuple(rows), tuple(issues)


@dataclass(frozen=True)
class ArgvResolution:
    """Execution-time re-resolution of one non-Claude sid (fresh evidence)."""

    session: Session | None = None
    detail: str = ""

    @property
    def success(self) -> bool:
        return self.session is not None


def resolve_argv_execution(provider_key: str, sid: str) -> ArgvResolution:
    """Re-resolve one non-Claude sid against fresh disk + `/proc` evidence.

    Mirror of the Claude execution-time resolver's guarantees (CLAUDE.md):
    a live takeover may only proceed on a freshly re-scanned whole Session —
    never on snapshot identity. Refuses missing sids, the current session,
    unusable cwds, and incomplete argv-walk evidence (fail closed, R10-like).
    """
    provider = get(provider_key)
    if not isinstance(provider, DiskDiscovery):
        return ArgvResolution(
            detail=f"provider {provider_key!r} has no execution-time discovery",
        )
    ancestors = proc.probe_current_ancestors()
    if not ancestors.complete:
        detail = "; ".join(i.detail for i in ancestors.issues)
        return ArgvResolution(detail=f"ancestor evidence incomplete: {detail}")
    inventory = proc.scan_cli_argv_inventory(frozenset({provider.basename}))
    if not inventory.complete:
        detail = "; ".join(i.detail for i in inventory.issues)
        return ArgvResolution(detail=f"process evidence incomplete: {detail}")
    scan = provider.discover(inventory, ancestors.pids)
    if not scan.complete:
        detail = "; ".join(i.detail for i in scan.issues)
        return ArgvResolution(detail=f"session discovery incomplete: {detail}")
    matches = tuple(row for row in scan.sessions if row.sid == sid)
    if not matches:
        return ArgvResolution(detail=f"missing session id {sid!r}")
    if len(matches) != 1:
        return ArgvResolution(
            detail=f"ambiguous session id {sid!r}; found {len(matches)} matches",
        )
    target = matches[0]
    if target.current:
        return ArgvResolution(detail=f"session {sid!r} is the current session")
    if not target.cwd or not os.path.isdir(target.cwd):
        return ArgvResolution(
            detail=f"session {sid!r} has no usable execution-time cwd: "
            f"{target.cwd!r}",
        )
    return ArgvResolution(session=target)
