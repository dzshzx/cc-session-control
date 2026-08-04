"""Provider registry — key → AgentProvider (ADR-0005).

The static table below is the single provider authority. A provider is
*active* when the operator allows it (`cfg.providers`, env `CSCTL_PROVIDERS`)
AND its CLI home directory exists; activation never fails the refresh — an
absent CLI simply contributes no rows.
"""

from __future__ import annotations

from .base import AgentProvider, DiskDiscovery, LivenessGrade, ProviderCaps, ProviderScan
from .claude import ClaudeProvider

__all__ = [
    "AgentProvider",
    "DiskDiscovery",
    "LivenessGrade",
    "ProviderCaps",
    "ProviderScan",
    "all_providers",
    "active_providers",
    "get",
]

_ALL: tuple[AgentProvider, ...] = (ClaudeProvider(),)


def all_providers() -> tuple[AgentProvider, ...]:
    return _ALL


def active_providers() -> tuple[AgentProvider, ...]:
    """Allowed AND locally present, in registry order (claude first)."""
    from ..config import cfg

    return tuple(
        p for p in _ALL if p.key in cfg.providers and p.available()
    )


def get(key: str) -> AgentProvider:
    """The provider owning `key`; raises KeyError for an unknown provider —
    an unknown `Session.provider` is a programming error, not a degraded
    source, so it must stay loud."""
    for p in _ALL:
        if p.key == key:
            return p
    raise KeyError(f"unknown provider {key!r}")
