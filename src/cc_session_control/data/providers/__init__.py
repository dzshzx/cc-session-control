"""Provider registry — key → AgentProvider (ADR-0005).

The static table below is the single provider authority. A provider is
*active* when the operator allows it (`cfg.providers`, env `CSCTL_PROVIDERS`)
AND its CLI home state exists; activation never fails the refresh — an
absent CLI simply contributes no rows.
"""

from __future__ import annotations

import os.path
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace

from ...models import InventoryIssue, Session
from .. import proc, tmux
from .base import (
    AgentProvider,
    ArchiveVerbs,
    CliDeleteResult,
    CliDeleteStage,
    CliDeleteState,
    DeleteVerbs,
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
    "ArchiveVerbs",
    "CliDeleteResult",
    "CliDeleteStage",
    "CliDeleteState",
    "DeleteVerbs",
    "DiskDiscovery",
    "LivenessGrade",
    "ProviderCaps",
    "ProviderScan",
    "ArgvResolution",
    "all_providers",
    "active_providers",
    "get",
    "is_active",
    "merge_sessions",
    "scan_non_claude",
    "resolve_argv_execution",
    "find_owning_provider",
    "unarchive_argv",
    "execute_cli_delete",
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


def is_active(key: str) -> bool:
    """Whether `key` is allowed AND locally present — THE activation
    predicate (launcher gates read this, never re-derive)."""
    return any(p.key == key for p in active_providers())


def merge_sessions(*row_groups: Iterable[Session]) -> tuple[Session, ...]:
    """THE one cross-provider merge: newest-first by mtime (snapshot and the
    headless resume listing both consume this, never re-sort inline)."""
    merged = [row for group in row_groups for row in group]
    merged.sort(key=lambda r: r.mtime, reverse=True)
    return tuple(merged)


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


def unarchive_argv(key: str, sid: str) -> list[str]:
    """THE un-archive argv dispatch (`session_ops.resume_cmd`'s archived
    branch consumes it): loud on a provider without archive verbs — only
    `ArchiveVerbs` providers ever mark rows `Session.archived`, so a
    mismatch is a programming error, not renderable uncertainty."""
    provider = get(key)
    if not isinstance(provider, ArchiveVerbs):
        raise TypeError(f"provider {key!r} has no archive verbs")
    return provider.unarchive_argv(sid)


def _delete_refusal(stage: CliDeleteStage, detail: str) -> CliDeleteResult:
    return CliDeleteResult(CliDeleteState.REFUSED, stage, detail)


def execute_cli_delete(provider_key: str, sid: str) -> CliDeleteResult:
    """Execution-time protection + delegated official delete (Sessions `d`).

    Mirror of `resolve_argv_execution`'s fresh-evidence discipline: the CLI
    only ever runs against a freshly re-scanned row that is confirmed dead,
    not current, and not archived (`codex delete` against the archived store
    is unverified upstream semantics — B7's refusal chain holds). Incomplete
    ancestors / argv-walk / discovery evidence refuses (fail closed, R10 —
    the same `probe_current_ancestors().complete` gate the cleanup family
    keys on). Loud on a provider without delete verbs: only `DeleteVerbs`
    rows ever reach this dispatch, so a mismatch is a programming error."""
    provider = get(provider_key)
    if not isinstance(provider, DeleteVerbs):
        raise TypeError(f"provider {provider_key!r} has no delete verbs")
    if not isinstance(provider, DiskDiscovery):
        return _delete_refusal(
            CliDeleteStage.EVIDENCE,
            f"provider {provider_key!r} has no execution-time discovery",
        )
    ancestors = proc.probe_current_ancestors()
    if not ancestors.complete:
        detail = "; ".join(i.detail for i in ancestors.issues)
        return _delete_refusal(
            CliDeleteStage.EVIDENCE, f"ancestor evidence incomplete: {detail}"
        )
    inventory = proc.scan_cli_argv_inventory(frozenset({provider.basename}))
    if not inventory.complete:
        detail = "; ".join(i.detail for i in inventory.issues)
        return _delete_refusal(
            CliDeleteStage.EVIDENCE, f"process evidence incomplete: {detail}"
        )
    scan = provider.discover(inventory, ancestors.pids)
    if not scan.complete:
        detail = "; ".join(i.detail for i in scan.issues)
        return _delete_refusal(
            CliDeleteStage.EVIDENCE, f"session discovery incomplete: {detail}"
        )
    matches = tuple(row for row in scan.sessions if row.sid == sid)
    if not matches:
        return _delete_refusal(
            CliDeleteStage.PROTECTION,
            f"session {sid!r} not found in fresh discovery",
        )
    if len(matches) != 1:
        return _delete_refusal(
            CliDeleteStage.PROTECTION,
            f"ambiguous session id {sid!r}; found {len(matches)} matches",
        )
    target = matches[0]
    if target.current:
        return _delete_refusal(
            CliDeleteStage.PROTECTION, f"session {sid!r} is the current session"
        )
    if target.alive:
        return _delete_refusal(
            CliDeleteStage.PROTECTION, f"session {sid!r} is live; stop it first"
        )
    if target.archived:
        return _delete_refusal(
            CliDeleteStage.PROTECTION,
            f"session {sid!r} is archived; unarchive it first",
        )
    return provider.delete_session_result(sid)


def find_owning_provider(sid: str) -> AgentProvider | None:
    """Best-effort: which active non-Claude provider's disk records contain
    `sid`? Liveness is irrelevant here (an empty `/proc` argv inventory and
    an empty ancestor set are fine) — this only answers "does this sid
    belong to codex/kimi" to enrich an already-failed Claude-only lookup
    (e.g. the `--take-over` rejection message), never to gate a new
    decision. Per-provider discovery issues are intentionally dropped: this
    is an error-path hint on top of a lookup that already failed, not a new
    fact source that must itself stay complete."""
    empty_inventory = proc.ProcCliInventory()
    for provider in active_providers():
        if provider.key == "claude" or not isinstance(provider, DiskDiscovery):
            continue
        scan = provider.discover(empty_inventory, frozenset())
        if any(row.sid == sid for row in scan.sessions):
            return provider
    return None


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
    if not provider.caps.takeover:
        return ArgvResolution(
            detail=f"provider {provider_key!r} does not support takeover",
        )
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
            detail=f"session {sid!r} has no usable execution-time cwd: {target.cwd!r}",
        )
    return ArgvResolution(session=_with_residency(target))


def _with_residency(target: Session) -> Session:
    """Fill tmux residency on a freshly re-resolved live row.

    Without this the model defaults (`tmux_target=None`,
    `tmux_inventory_complete=True`) would FABRICATE completeness: the
    spawn-path guard and the in-place attach check would silently no-op and a
    resident session could be SIGTERMed instead of entered (same guarantee as
    the Claude resolver, whose scan injects residency itself)."""
    if not target.alive or not target.pid:
        return target
    inventory = tmux.residency_inventory({target.pid})
    return replace(
        target,
        tmux_target=inventory.targets.get(target.pid),
        tmux_inventory_complete=inventory.complete,
        tmux_inventory_detail=inventory.issue_detail,
    )
